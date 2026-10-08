/* Stat tile: label (sentence case) + value + optional sub-line.
 *
 * Values use the font's default proportional figures. tabular-nums is
 * reserved for columns that align vertically (table rows, axis ticks) --
 * on a large standalone number it makes something like 121 look loose.
 */
export function Stat({
  label,
  value,
  sub,
}: {
  label: string;
  value: string | number;
  sub?: string;
}) {
  return (
    <div className="stat">
      <div className="label">{label}</div>
      <div className="value">
        {typeof value === "number" ? value.toLocaleString() : value}
      </div>
      {sub && <div className="sub">{sub}</div>}
    </div>
  );
}
