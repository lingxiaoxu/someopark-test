import { z } from 'zod'

export const stanseAgentSchema = z.object({
  commentary: z
    .string()
    .describe(
      `Describe what you're about to do and the steps you want to take for generating the code in great detail.`,
    ),
  template: z
    .string()
    .describe('Name of the template used to generate the code.'),
  title: z.string().describe('Short title of the generated code. Max 3 words.'),
  description: z
    .string()
    .describe('Short description of the generated code. Max 1 sentence.'),
  additional_dependencies: z
    .array(z.string())
    .describe(
      'Additional dependencies required by the code. Do not include dependencies that are already included in the template.',
    ),
  has_additional_dependencies: z
    .boolean()
    .describe(
      'Detect if additional dependencies that are not included in the template are required.',
    ),
  install_dependencies_command: z
    .string()
    .describe(
      'Command to install additional dependencies required by the code.',
    ),
  port: z
    .number()
    .nullable()
    .describe(
      'Port number used by the resulted app. Null when no ports are exposed.',
    ),
  file_path: z
    .string()
    .describe('Relative path to the file, including the file name.'),
  code: z
    .string()
    .describe('Code generated. Only runnable code is allowed.'),
})

export type StanseAgentSchema = z.infer<typeof stanseAgentSchema>

export const morphEditSchema = z.object({
  edit: z
    .string()
    .describe(
      'The modified code with changes. Use "# ... existing code ..." markers for unchanged sections. THIS FIELD IS REQUIRED.',
    ),
  commentary: z
    .string()
    .optional()
    .describe('Explain what changes you are making and why'),
  instruction: z
    .string()
    .optional()
    .describe('One line instruction on what the change is'),
  file_path: z
    .string()
    .optional()
    .describe('Path to the file being edited'),
  new_dependencies: z
    .array(z.string())
    .optional()
    .describe(
      'New pip/npm packages required by the code changes.',
    ),
})

export type MorphEditSchema = z.infer<typeof morphEditSchema>
