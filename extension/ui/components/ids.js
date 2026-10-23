// Unique element ids for label/aria wiring inside components.

let counter = 0;

export function uniqueId(prefix = "snag") {
  counter += 1;
  return `${prefix}-${counter}`;
}
