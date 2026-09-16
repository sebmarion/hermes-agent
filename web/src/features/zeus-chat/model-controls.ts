export interface ModelChoice { provider: string; providerName: string; model: string; warning: string }
export interface ModelOptions { model: string; provider: string; choices: ModelChoice[] }
export interface ModelSwitchResult { key?: string; value?: string; scope?: string; deferred?: boolean; confirm_required?: boolean; confirm_message?: string; warning?: string }
const text = (value: unknown) => typeof value === "string" ? value : "";
/** Only catalogue IDs can become model-switch arguments; never accept arbitrary slash commands. */
export function modelSwitchValue(choice: ModelChoice): string {
  if (!choice.model || choice.model.length > 300 || !/^[a-zA-Z0-9][a-zA-Z0-9._:/@+\[\]-]*$/.test(choice.model) || !/^[a-zA-Z0-9][a-zA-Z0-9._-]*$/.test(choice.provider)) throw new Error("This model identifier is not supported by the selector.");
  return `${choice.model} --provider ${choice.provider} --session`;
}
export function parseModelOptions(value: unknown): ModelOptions {
  if (!value || typeof value !== "object") throw new Error("The model catalogue could not be read.");
  const raw = value as Record<string, unknown>;
  if (!Array.isArray(raw.providers)) throw new Error("The model catalogue is unavailable. Retry to load it.");
  const choices: ModelChoice[] = [];
  for (const entry of raw.providers) {
    if (!entry || typeof entry !== "object") continue;
    const p = entry as Record<string, unknown>;
    if (!Array.isArray(p.models)) continue;
    for (const model of p.models) {
      const choice = { model: text(model), provider: text(p.slug), providerName: text(p.name) || text(p.slug), warning: text(p.warning) };
      try { modelSwitchValue(choice); choices.push(choice); } catch { /* Unusable IDs are not switch controls. */ }
    }
  }
  return { model: text(raw.model), provider: text(raw.provider), choices };
}
export const choiceKey = (choice: Pick<ModelChoice, "provider" | "model">) => `${choice.provider}\0${choice.model}`;
