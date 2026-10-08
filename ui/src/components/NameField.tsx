import { NAME_PATTERN, slugifyName } from "../api";

/* The name field for a trigger or a rule.
 *
 * Names are identifiers -- YAML keys, what a rule's `trigger` points at, the
 * key the fire ledger carries -- so they are constrained. The constraint is
 * fine; discovering it by submitting and getting "422 Unprocessable Entity"
 * back was not.
 *
 * So the rule is explained where it is broken, and the name the person almost
 * certainly meant is one click away. Deliberately not auto-corrected as they
 * type: silently rewriting what someone typed is its own kind of rude, and a
 * name is the one thing here they will have to recognise again later.
 */
export function NameField({
  id,
  label = "Name",
  value,
  placeholder,
  onChange,
}: {
  id: string;
  label?: string;
  value: string;
  placeholder: string;
  onChange: (next: string) => void;
}) {
  const suggestion = slugifyName(value);
  const invalid = value.length > 0 && !NAME_PATTERN.test(value);

  return (
    <div>
      <label className="field" htmlFor={id}>
        {label}
      </label>
      <input
        id={id}
        type="text"
        value={value}
        placeholder={placeholder}
        aria-invalid={invalid || undefined}
        aria-describedby={invalid ? `${id}-hint` : undefined}
        onChange={(e) => onChange(e.target.value)}
      />
      {invalid && (
        <p className="footnote bad-text" id={`${id}-hint`} style={{ marginTop: 6 }}>
          Lowercase letters, numbers and underscores, starting with a letter.
          {suggestion && suggestion !== value && (
            <>
              {" "}
              <button
                type="button"
                className="linkish"
                onClick={() => onChange(suggestion)}
              >
                use {suggestion}
              </button>
            </>
          )}
        </p>
      )}
    </div>
  );
}
