import type { TableInfo } from "../api";

/* The schema, as reference material for someone writing SQL.
 *
 * Read live from the database rather than from a stored model: what a trigger
 * can reference is whatever the role can see, and a parallel description would
 * be one more thing to keep in step for no gain.
 *
 * Structure is shown; meaning is not guessed. Nothing here claims to know what
 * `status = 3` means, because a wrong guess produces a trigger that looks
 * correct and fires on the wrong rows.
 */
export function SchemaBrowser({
  tables,
  openByDefault = false,
}: {
  tables: TableInfo[];
  openByDefault?: boolean;
}) {
  return (
    <>
      {tables.map((table) => (
        <details className="entity" key={table.name} open={openByDefault}>
          <summary>
            <span className="caret" aria-hidden="true">
              ▶
            </span>
            <strong>{table.name}</strong>
            {table.primary_key ? (
              <span className="tag">pk: {table.primary_key}</span>
            ) : (
              <span className="tag todo">no single-column pk</span>
            )}
            {table.timestamps.length > 0 && (
              <span className="tag">clocks: {table.timestamps.slice(0, 3).join(", ")}</span>
            )}
            <span className="spacer" />
            <span className="muted" style={{ fontSize: 12 }}>
              {table.columns.length} columns
            </span>
          </summary>

          <div className="body">
            <div className="table-scroll">
              <table>
                <thead>
                  <tr>
                    <th>column</th>
                    <th>postgres type</th>
                    <th>reads as</th>
                    <th>null</th>
                  </tr>
                </thead>
                <tbody>
                  {table.columns.map((column) => (
                    <tr key={column.name}>
                      <td>{column.name}</td>
                      <td className="muted">{column.pg_type}</td>
                      <td>{column.semantic_type}</td>
                      <td className="muted">{column.nullable ? "yes" : "no"}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>

            {table.foreign_keys.length > 0 && (
              <p className="footnote" style={{ marginTop: 10 }}>
                joins:{" "}
                {table.foreign_keys
                  .map((fk) => `${fk.column} → ${fk.target_table}.${fk.target_column}`)
                  .join(" · ")}
              </p>
            )}
          </div>
        </details>
      ))}
    </>
  );
}
