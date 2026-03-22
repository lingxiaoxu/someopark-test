export async function applyPatch({
  targetFile,
  instructions,
  initialCode,
  codeEdit,
  apiKey,
}: {
  targetFile: string
  instructions: string
  initialCode: string
  codeEdit: string
  apiKey?: string
}) {
  const morphApiKey = apiKey || process.env.MORPH_API_KEY

  if (!morphApiKey) {
    throw new Error(
      'Morph API key is required. Please add it in settings or set MORPH_API_KEY environment variable.',
    )
  }

  const prompt = `<instruction>${instructions}</instruction>\n<code>${initialCode}</code>\n<update>${codeEdit}</update>`

  try {
    const response = await fetch('https://api.morphllm.com/v1/chat/completions', {
      method: 'POST',
      headers: {
        'Content-Type': 'application/json',
        Authorization: `Bearer ${morphApiKey}`,
      },
      body: JSON.stringify({
        model: 'morph-v3-large',
        messages: [{ role: 'user', content: prompt }],
      }),
    })

    if (!response.ok) {
      const errorText = await response.text()
      throw new Error(`Morph API error ${response.status}: ${errorText}`)
    }

    const data = await response.json() as any
    const mergedCode = data?.choices?.[0]?.message?.content

    if (!mergedCode) {
      throw new Error('Morph Apply returned empty content')
    }

    return {
      filePath: targetFile,
      code: mergedCode,
    }
  } catch (error: any) {
    if (error.message.includes('Invalid API key') || error.message.includes('401')) {
      throw new Error('Invalid Morph API key. Please check your settings.')
    }
    throw new Error(`Failed to apply morph: ${error.message}`)
  }
}
