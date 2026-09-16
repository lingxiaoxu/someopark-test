// Public-web GET transport shared by web_fetch and http_request.
// Resolve and pin each connection, including redirects; validating a URL before
// an ordinary fetch is insufficient because fetch can resolve it again.
import { lookup } from 'node:dns/promises'
import { request as httpRequest, type IncomingHttpHeaders } from 'node:http'
import { request as httpsRequest } from 'node:https'
import { BlockList, isIP } from 'node:net'
import { brotliDecompress, gunzip, inflate } from 'node:zlib'
import { promisify } from 'node:util'

export const MAX_HTTP_BYTES = 10 * 1024 * 1024
export const MAX_HTTP_TIMEOUT_MS = 60_000
const MAX_REDIRECTS = 10
const privateIPs = new BlockList()
for (const [network, prefix] of [
  ['0.0.0.0', 8], ['10.0.0.0', 8], ['100.64.0.0', 10], ['127.0.0.0', 8],
  ['169.254.0.0', 16], ['172.16.0.0', 12], ['192.0.0.0', 24],
  ['192.0.2.0', 24], ['192.88.99.0', 24], ['192.168.0.0', 16],
  ['198.18.0.0', 15], ['198.51.100.0', 24], ['203.0.113.0', 24],
  ['224.0.0.0', 4], ['240.0.0.0', 4],
] as const) privateIPs.addSubnet(network, prefix, 'ipv4')
const publicIPv6 = new BlockList()
publicIPv6.addSubnet('2000::', 3, 'ipv6')
for (const [network, prefix] of [
  ['2001::', 32], ['2001:2::', 48], ['2001:10::', 28],
  ['2001:20::', 28], ['2001:db8::', 32], ['2002::', 16], ['3fff::', 20],
] as const) privateIPs.addSubnet(network, prefix, 'ipv6')

export function isPublicAddress(address: string): boolean {
  const family = isIP(address)
  if (family === 4) return !privateIPs.check(address, 'ipv4')
  // Also excludes loopback, mapped IPv4, link-local, multicast, ULA and NAT64.
  return family === 6 && publicIPv6.check(address, 'ipv6') && !privateIPs.check(address, 'ipv6')
}

function validateURL(value: string): URL {
  if (typeof value !== 'string' || value.length > 8192) throw new Error('Invalid URL')
  const url = new URL(value)
  if (!['http:', 'https:'].includes(url.protocol)) throw new Error('Only HTTP and HTTPS URLs are supported')
  if (url.username || url.password) throw new Error('URL credentials are not allowed')
  const hostname = url.hostname.replace(/^\[|\]$/g, '').replace(/\.$/, '').toLowerCase()
  if (!hostname || hostname === 'localhost' || hostname.endsWith('.localhost') ||
      hostname.endsWith('.local') || (!isIP(hostname) && !hostname.includes('.'))) {
    throw new Error('Requests to localhost/private hosts are not allowed')
  }
  return url
}

type Address = { address: string; family: number }
export type SafeResponse = { status: number; contentType: string; text: string; url: string }
type HopResponse = { status: number; headers: IncomingHttpHeaders; body: Buffer }
type HopOptions = { signal: AbortSignal; headers: Record<string, string> }
export interface SafeFetchDependencies {
  lookup: (hostname: string) => Promise<Address[]>
  request: (url: URL, address: Address, options: HopOptions) => Promise<HopResponse>
}

function requestPinned(url: URL, address: Address, options: HopOptions): Promise<HopResponse> {
  return new Promise((resolve, reject) => {
    const request = url.protocol === 'https:' ? httpsRequest : httpRequest
    const req = request(url, {
      method: 'GET', signal: options.signal, family: address.family,
      // Keep the original host for HTTP Host / TLS certificate verification.
      lookup: (_hostname, _options, callback) => callback(null, address.address, address.family),
      headers: { ...options.headers, 'accept-encoding': 'identity' },
    }, res => {
      const status = res.statusCode || 0
      if ([301, 302, 303, 307, 308].includes(status)) {
        res.destroy()
        resolve({ status, headers: res.headers, body: Buffer.alloc(0) })
        return
      }
      const chunks: Buffer[] = []
      let size = 0
      res.on('data', (chunk: Buffer) => {
        size += chunk.length
        if (size > MAX_HTTP_BYTES) {
          const error = new Error(`HTTP response exceeds ${MAX_HTTP_BYTES} bytes`)
          res.destroy(error)
          req.destroy(error)
          return
        }
        chunks.push(chunk)
      })
      res.on('error', reject)
      res.on('aborted', () => reject(new Error('HTTP response was interrupted')))
      res.on('end', () => resolve({ status, headers: res.headers, body: Buffer.concat(chunks) }))
    })
    req.on('error', reject)
    req.end()
  })
}

const defaults: SafeFetchDependencies = {
  lookup: hostname => lookup(hostname, { all: true, verbatim: true }),
  request: requestPinned,
}

function abortable<T>(promise: Promise<T>, signal: AbortSignal): Promise<T> {
  if (signal.aborted) return Promise.reject(signal.reason || new Error('Request aborted'))
  return new Promise((resolve, reject) => {
    const onAbort = () => reject(signal.reason || new Error('Request aborted'))
    signal.addEventListener('abort', onAbort, { once: true })
    promise.then(resolve, reject).finally(() => signal.removeEventListener('abort', onAbort))
  })
}

export async function fetchSafeText(
  value: string,
  options: { headers?: Record<string, string>; timeout?: number; signal?: AbortSignal } = {},
  dependencies: SafeFetchDependencies = defaults,
): Promise<SafeResponse> {
  const timeout = options.timeout ?? 10_000
  if (!Number.isFinite(timeout) || timeout <= 0) throw new Error('Timeout must be a positive number')
  const controller = new AbortController()
  const timer = setTimeout(() => controller.abort(new Error('HTTP request timed out')), Math.min(timeout, MAX_HTTP_TIMEOUT_MS))
  timer.unref()
  const signal = options.signal ? AbortSignal.any([controller.signal, options.signal]) : controller.signal
  let headers = { ...options.headers }
  // Do not allow a caller to override the validated host or request framing.
  for (const name of Object.keys(headers)) {
    if (['host', 'connection', 'content-length', 'transfer-encoding'].includes(name.toLowerCase())) delete headers[name]
  }
  try {
    let url = validateURL(value)
    for (let hop = 0; hop <= MAX_REDIRECTS; hop++) {
      signal.throwIfAborted()
      const hostname = url.hostname.replace(/^\[|\]$/g, '')
      const family = isIP(hostname)
      const addresses = family ? [{ address: hostname, family }] : await abortable(dependencies.lookup(hostname), signal)
      if (!addresses.length || addresses.some(({ address }) => !isPublicAddress(address))) {
        throw new Error('Requests to localhost/private or reserved IPs are not allowed')
      }
      const response = await abortable(dependencies.request(url, addresses[0], { signal, headers }), signal)
      if ([301, 302, 303, 307, 308].includes(response.status)) {
        if (!response.headers.location) throw new Error('HTTP redirect is missing Location')
        if (hop === MAX_REDIRECTS) throw new Error('Too many HTTP redirects')
        const nextURL = validateURL(new URL(response.headers.location, url).href)
        // Never leak caller-provided credentials/headers to a new origin.
        if (nextURL.origin !== url.origin) headers = {}
        url = nextURL
        continue
      }
      if (response.body.length > MAX_HTTP_BYTES) throw new Error('HTTP response is too large')
      let body = response.body
      const encoding = String(response.headers['content-encoding'] || 'identity').toLowerCase()
      const decoders = { gzip: promisify(gunzip), br: promisify(brotliDecompress), deflate: promisify(inflate) }
      if (encoding !== 'identity') {
        const decode = decoders[encoding]
        if (!decode) throw new Error(`Unsupported HTTP content encoding: ${encoding}`)
        body = await abortable(decode(body, { maxOutputLength: MAX_HTTP_BYTES }), signal)
      }
      signal.throwIfAborted()
      return { status: response.status, contentType: String(response.headers['content-type'] || ''), text: body.toString('utf8'), url: url.href }
    }
    throw new Error('Too many HTTP redirects')
  } finally {
    clearTimeout(timer)
  }
}
