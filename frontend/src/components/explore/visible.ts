// Bidirectional controls can visually reorder code (Trojan Source). Show them, never apply them.
const BIDI = /[\u061c\u200e\u200f\u202a-\u202e\u2066-\u2069]/g

export function visible(text: string): string {
  return text.replace(BIDI, character => `⟪U+${character.codePointAt(0)?.toString(16).toUpperCase().padStart(4, '0')}⟫`)
}
