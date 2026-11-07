# Code standards

## Comments

- A comment says something the code cannot say about itself. Everything else is deleted.
- The code says what and how. A comment says why.
- A comment may say what, in one line, where the code is illegible: a regex, a protocol offset, a non-obvious algorithm.
- A comment never restates a line that already reads clearly.

### Never appears in a comment

Prohibited outright, in every language, in docstrings as well as inline:

- **Prose.** A comment is not an essay. If it needs a paragraph, the code needs a better name or a smaller function.
- **Decisions and design discussion.** The case for one design over another. A comment states the constraint the code is under, not the argument for it.
- **People.** No names, no initials, no "requested by", no "per our discussion", no attribution of a choice to anyone.
- **Process and provenance.** No session numbers, task or backlog IDs, sprint references, plan or spec filenames, dates of changes, "as of the refactor", "previously this did X".
- **Tooling.** No mention of the editor, generator or other tool used to write the code.
- **Changelog prose.** "Added in v2", "changed to fix the timeout bug", "used to call `_legacy_parse`". Version control holds history; a comment holding history goes stale silently.
- **Commented-out code.** Delete it.

### Bug-fix comments

- Allowed when the comment explains a workaround a future reader would otherwise undo.
- States the constraint, not the incident.
- Allowed: "the registry returns 200 with an empty body when the token has expired, so status alone is not sufficient".
- Not allowed: "fixes the bug found when the sync ran twice".

### Where a comment does earn its place

- Code that looks wrong or redundant and is not, so the next reader does not "fix" it.
- A constraint on the caller the signature cannot express: ordering, lifetime, task affinity, a unit, a range.
- A workaround for an external defect, with a link to the upstream issue.
- A source link for code adapted from elsewhere.
- A deliberate omission, and what would have to be true to fill it in.
- A security or correctness invariant the surrounding code depends on and does not enforce.

### Size

| Scope | Allowance |
|---|---|
| Module or file header | One to three lines. What this file is. Longer belongs in the wiki. |
| Public function or class | A one-line docstring, always, in the imperative. |
| `Args:`/`Returns:`/`Raises:` | Only where they add what the signature and type hints do not. A section restating an annotated parameter is deleted. |
| Private helper | No docstring when the name and body are self-evident. |
| Inline | One line. |

### Handling

- **String literals are code, not comments.** User-facing copy, log messages, SQL, schema descriptions and validation text are data the program depends on. Never edit inside a string while reducing comments.
- **A comment you invalidate is fixed or deleted.** Changing code and leaving its comment behind leaves a comment that is wrong. Deleting without replacing is always correct in that case.
- **`TODO` is allowed** when it names the condition that would resolve it. No person, no date, no ticket reference.
