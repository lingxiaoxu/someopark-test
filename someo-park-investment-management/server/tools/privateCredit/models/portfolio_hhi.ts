import { ModelDefinition, ModelResult } from './types.js'

interface Position {
  name: string
  gp: string
  sector: string
  vintage: string
  exposure: number
}

const model: ModelDefinition = {
  name: 'portfolio_hhi',
  sheetName: 'Portfolio HHI Concentration',
  description:
    'Herfindahl-Hirschman Index (HHI) concentration analysis across multiple dimensions: position, GP, sector, and vintage.',
  fund: 'General',

  inputs: [
    {
      name: 'positions', label: 'Positions', type: 'array',
      default: [
        { name: 'Fund A', gp: 'GP1', sector: 'Tech', vintage: '2022', exposure: 30 },
        { name: 'Fund B', gp: 'GP1', sector: 'Healthcare', vintage: '2023', exposure: 25 },
        { name: 'Fund C', gp: 'GP2', sector: 'Tech', vintage: '2022', exposure: 20 },
        { name: 'Fund D', gp: 'GP3', sector: 'Energy', vintage: '2024', exposure: 15 },
        { name: 'Fund E', gp: 'GP4', sector: 'Healthcare', vintage: '2023', exposure: 10 },
      ] as any,
      description: 'Array of positions with name, gp, sector, vintage, exposure',
    },
  ],

  outputs: [
    { name: 'hhi_position', label: 'HHI by Position', type: 'number' },
    { name: 'hhi_gp', label: 'HHI by GP', type: 'number' },
    { name: 'hhi_sector', label: 'HHI by Sector', type: 'number' },
    { name: 'hhi_vintage', label: 'HHI by Vintage', type: 'number' },
    { name: 'effective_n_position', label: 'Effective N (Position)', type: 'number' },
    { name: 'effective_n_gp', label: 'Effective N (GP)', type: 'number' },
    { name: 'concentration_flag', label: 'Concentration Flag', type: 'string' },
  ],

  compute(inputs: Record<string, any>): ModelResult {
    const positions: Position[] = inputs.positions ?? [
      { name: 'Fund A', gp: 'GP1', sector: 'Tech', vintage: '2022', exposure: 30 },
      { name: 'Fund B', gp: 'GP1', sector: 'Healthcare', vintage: '2023', exposure: 25 },
      { name: 'Fund C', gp: 'GP2', sector: 'Tech', vintage: '2022', exposure: 20 },
      { name: 'Fund D', gp: 'GP3', sector: 'Energy', vintage: '2024', exposure: 15 },
      { name: 'Fund E', gp: 'GP4', sector: 'Healthcare', vintage: '2023', exposure: 10 },
    ]

    const totalExposure = positions.reduce((sum, p) => sum + p.exposure, 0)
    if (totalExposure === 0) {
      return {
        model: model.name,
        inputs: { positions: positions.length } as any,
        outputs: {
          hhi_position: 0, hhi_gp: 0, hhi_sector: 0, hhi_vintage: 0,
          effective_n_position: 0, effective_n_gp: 0,
          concentration_flag: 'No positions',
        },
      }
    }

    // Helper: compute HHI for a grouping
    function computeHHI(groups: Map<string, number>): number {
      let hhi = 0
      for (const exposure of groups.values()) {
        const weight = exposure / totalExposure
        hhi += weight * weight
      }
      return hhi
    }

    // Position-level HHI
    const posMap = new Map<string, number>()
    for (const p of positions) {
      posMap.set(p.name, (posMap.get(p.name) ?? 0) + p.exposure)
    }
    const hhiPosition = computeHHI(posMap)

    // GP-level HHI
    const gpMap = new Map<string, number>()
    for (const p of positions) {
      gpMap.set(p.gp, (gpMap.get(p.gp) ?? 0) + p.exposure)
    }
    const hhiGP = computeHHI(gpMap)

    // Sector-level HHI
    const sectorMap = new Map<string, number>()
    for (const p of positions) {
      sectorMap.set(p.sector, (sectorMap.get(p.sector) ?? 0) + p.exposure)
    }
    const hhiSector = computeHHI(sectorMap)

    // Vintage-level HHI
    const vintageMap = new Map<string, number>()
    for (const p of positions) {
      vintageMap.set(p.vintage, (vintageMap.get(p.vintage) ?? 0) + p.exposure)
    }
    const hhiVintage = computeHHI(vintageMap)

    const effectiveNPosition = hhiPosition > 0 ? 1 / hhiPosition : 0
    const effectiveNGP = hhiGP > 0 ? 1 / hhiGP : 0

    // Concentration flags
    const flags: string[] = []
    if (hhiPosition > 0.25) flags.push(`Position HHI ${(hhiPosition * 10000).toFixed(0)} > 2500`)
    if (hhiGP > 0.25) flags.push(`GP HHI ${(hhiGP * 10000).toFixed(0)} > 2500`)
    if (hhiSector > 0.25) flags.push(`Sector HHI ${(hhiSector * 10000).toFixed(0)} > 2500`)
    if (hhiVintage > 0.25) flags.push(`Vintage HHI ${(hhiVintage * 10000).toFixed(0)} > 2500`)

    return {
      model: model.name,
      inputs: { positions: positions.length } as any,
      outputs: {
        hhi_position: hhiPosition,
        hhi_gp: hhiGP,
        hhi_sector: hhiSector,
        hhi_vintage: hhiVintage,
        effective_n_position: effectiveNPosition,
        effective_n_gp: effectiveNGP,
        concentration_flag: flags.length > 0 ? flags.join('; ') : 'Diversified',
      },
      description: `Portfolio HHI: Position=${(hhiPosition * 10000).toFixed(0)}, GP=${(hhiGP * 10000).toFixed(0)}, Sector=${(hhiSector * 10000).toFixed(0)}, Vintage=${(hhiVintage * 10000).toFixed(0)}. Effective N=${effectiveNPosition.toFixed(1)} positions, ${effectiveNGP.toFixed(1)} GPs.`,
    }
  },
}

export default model
