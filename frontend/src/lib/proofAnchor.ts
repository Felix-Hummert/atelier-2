/** Short form a proof hash may show. The full value stays on the affordance. */
export function shortHash(value: string): string {
  if (value.length <= 12) return value;
  return `${value.slice(0, 8)}…${value.slice(-4)}`;
}
