# Command Input Navigation Design

## Purpose

Replace mode-switching keyboard shortcuts in the wmole TUI with a visible,
Claude Code-style command input. Users access the application's operational
surfaces by typing `/`, filtering a compact command list, and selecting an
operation with the keyboard.

## Scope

The command input provides access to these surfaces and actions:

- `/analyze`: filesystem explorer
- `/categories`: category-based disk analysis
- `/purge`: development artifact cleanup
- `/installers`: old installer scan
- `/uninstall`: installed applications and leftovers
- `/optimize`: maintenance operations
- `/status`: live status display
- `/ports`: listening development ports snapshot
- `/update`: update check
- `/help`: help and feature reference

The command input does not expose `/lang`, `/permanent`, or `/quit`. Language,
delete-mode, back/exit, item selection, navigation, and safety confirmation
controls remain contextual keyboard interactions outside the operation menu.

## User Experience

The bottom of each TUI view includes an always-visible input line. When idle,
it indicates that `/` opens operations. When the user presses `/`, the input
becomes active and a selectable list appears immediately above or adjacent to
the input within the existing terminal layout.

The active list shows each command name and its localized description. Further
typing filters by command name or description. The selected result is visually
distinct from the other results.

Input behavior:

- `/` starts command entry from any primary view.
- Printable characters refine the query.
- `Up` and `Down` select a visible matching command.
- `Enter` executes the selected command.
- `Backspace` edits the query.
- `Esc` clears command entry and closes results.

## Navigation Model

The direct top-level shortcuts `Shift+A`, `Shift+C`, `Shift+P`, `Shift+I`,
`Shift+U`, `Shift+M`, and `Shift+S` are removed as entry paths. The command
input becomes the only shortcut-driven access path between operational views.

Contextual controls remain unchanged because they operate within a selected
surface rather than replacing command navigation:

- List traversal and activation: `Up`, `Down`, `Enter`
- Selection and deletion within cleanable lists: `Space`, `D`
- Existing permanent-mode and dry-run controls
- Filesystem-specific actions such as large files, drives, and Explorer open
- Uninstall leftovers action
- Help closing and ordinary back/exit behavior

## Implementation Shape

The existing `palette_commands()`, `filter_palette()`, `render_palette()`, and
`run_tui()` palette state form the basis of the feature. Implementation should
make the command input persistently visible, restrict its command catalog to
the scoped operations, and delete only the mode-switching shortcut handlers
made redundant by the input.

The existing view dispatch behavior is retained for included commands.
Documentation and built-in help/footer content must describe command-input
navigation instead of advertising removed mode shortcuts.

## Safety And Error Handling

The new navigation path does not bypass current safeguards. Destructive
operations still use recycle-bin defaults, selected-target rules, protected
path filtering, permanent-mode state, and high-risk confirmation prompts.

If a command produces no matching results, the input list renders an explicit
empty result state and does not execute anything on `Enter`.

## Testing And Verification

Automated coverage should verify:

- The palette exposes only the approved command catalog.
- Filtering still returns operation matches and excludes removed actions.
- Footer/help content no longer describes the removed top-level shortcuts.

Manual TUI verification should exercise:

- Idle input visibility.
- `/` opening the results list from a normal view.
- Query filtering, result navigation, execution, and cancellation.
- Absence of direct mode navigation through the removed `Shift+` shortcuts.
- Preservation of one contextual workflow and one safety confirmation flow.

## Out Of Scope

- Converting in-view selection, deletion, or confirmation controls into slash
  commands.
- Adding mouse support or a graphical interface.
- Restructuring the single-file Python TUI beyond the focused input and
  navigation changes.
