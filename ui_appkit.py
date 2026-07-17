#!/usr/bin/env python3
"""Natives Einstellungsfenster (PyObjC/AppKit) — rumps-frei.

Portiert aus ``mailrelay.py``. Der Builder ist dort schon bewusst app-agnostisch
angelegt ("damit dieser Builder später unverändert in ein gemeinsames Modul wandern
kann") — Validierung und Persistenz stecken ausschließlich im ``on_commit``-Callback,
hier ist nichts phonebook-spezifisch. Wenn er je in ein geteiltes Modul wandert, kann
diese Datei ersatzlos entfallen.

Sections beschreiben das Fenster deklarativ:

    sections = [("Server", [("Port", "int", "port")], "Hinweistext")]
    run_settings_window("Titel", sections, {"port": 8081}, on_commit)

``on_commit(raw) -> list[str]``: leere Liste = übernehmen und schließen, sonst
Fehlertexte (Fenster bleibt offen, Beep).
"""

from __future__ import annotations

try:  # AppKit nur lazy/guarded – das Modul bleibt auch ohne GUI importierbar
    from Foundation import NSObject as _NSObject
    _HAVE_APPKIT = True
except Exception:  # pragma: no cover - umgebungsabhängig
    _NSObject = object
    _HAVE_APPKIT = False

_SETTINGS_OK = 1000
_SETTINGS_CANCEL = 0


class _SettingsController(_NSObject):  # type: ignore[misc]
    """Hält die Controls am Leben und bedient Speichern/Abbrechen (Target/Action) für die
    Dauer des modalen Fensters."""

    def ok_(self, _sender):
        import AppKit
        raw = {}
        for key, (kind, control) in self._controls.items():
            if kind == "check":
                raw[key] = control.state() == 1
            else:  # text | int | secret
                raw[key] = control.stringValue()
        errors = self._on_commit(raw)
        if errors:
            AppKit.NSBeep()
            self._error_label.setStringValue_("  •  ".join(errors))
            self._error_label.setHidden_(False)
            return  # modal offen lassen, damit der Nutzer korrigieren kann
        AppKit.NSApplication.sharedApplication().stopModalWithCode_(_SETTINGS_OK)

    def cancel_(self, _sender):
        import AppKit
        AppKit.NSApplication.sharedApplication().stopModalWithCode_(_SETTINGS_CANCEL)


def run_settings_window(title, sections, initial, on_commit):
    """Zeigt ein modales, feldgetriebenes Einstellungsfenster (Main-Thread).

    ``sections``: ``list[(section_title, rows, note|None)]`` mit
    ``rows = list[(label, kind, key)]``, ``kind ∈ text|int|secret|check``.
    ``initial``: ``dict`` key->``str``|``bool``. ``on_commit(raw) -> list[str]``: leere Liste
    = übernehmen + schließen, sonst Fehlertexte (Fenster bleibt offen, Beep).
    Rückgabe: ``True`` bei Speichern, ``False`` bei Abbrechen.
    """
    import AppKit
    from Foundation import NSMakeRect, NSMakeSize

    controller = _SettingsController.alloc().init()
    controller._controls = {}
    controller._on_commit = on_commit
    pending = []

    def _label(text, bold=False, dim=False):
        lbl = AppKit.NSTextField.labelWithString_(text)
        if bold:
            lbl.setFont_(AppKit.NSFont.boldSystemFontOfSize_(13))
        if dim:
            lbl.setTextColor_(AppKit.NSColor.secondaryLabelColor())
        return lbl

    def _make_control(kind, key):
        value = initial.get(key)
        if kind == "check":
            btn = AppKit.NSButton.checkboxWithTitle_target_action_("", None, None)
            btn.setState_(1 if value else 0)
            return btn
        cls = AppKit.NSSecureTextField if kind == "secret" else AppKit.NSTextField
        field = cls.alloc().init()
        field.setStringValue_("" if value is None else str(value))
        field.setTranslatesAutoresizingMaskIntoConstraints_(False)
        pending.append(field.widthAnchor().constraintGreaterThanOrEqualToConstant_(240))
        return field

    stack = AppKit.NSStackView.alloc().init()
    stack.setOrientation_(1)  # vertikal
    stack.setAlignment_(AppKit.NSLayoutAttributeLeading)
    stack.setSpacing_(10)
    stack.setTranslatesAutoresizingMaskIntoConstraints_(False)

    first_field = None
    for si, (section_title, rows, note) in enumerate(sections):
        if si > 0:
            sep = AppKit.NSBox.alloc().init()
            sep.setBoxType_(AppKit.NSBoxSeparator)
            stack.addArrangedSubview_(sep)
            pending.append(sep.widthAnchor().constraintEqualToAnchor_(stack.widthAnchor()))
        stack.addArrangedSubview_(_label(section_title, bold=True))
        grid_rows = []
        for label, kind, key in rows:
            control = _make_control(kind, key)
            controller._controls[key] = (kind, control)
            if first_field is None and kind not in ("check",):
                first_field = control
            grid_rows.append([_label(label + ":"), control])
        grid = AppKit.NSGridView.gridViewWithViews_(grid_rows)
        grid.setRowSpacing_(6)
        grid.setColumnSpacing_(8)
        grid.columnAtIndex_(0).setXPlacement_(AppKit.NSGridCellPlacementTrailing)
        stack.addArrangedSubview_(grid)
        if note:
            stack.addArrangedSubview_(_label(note, dim=True))

    error_label = _label("")
    error_label.setTextColor_(AppKit.NSColor.systemRedColor())
    error_label.setHidden_(True)
    controller._error_label = error_label
    stack.addArrangedSubview_(error_label)

    cancel_btn = AppKit.NSButton.buttonWithTitle_target_action_("Abbrechen", controller, "cancel:")
    cancel_btn.setKeyEquivalent_("\x1b")  # Esc
    ok_btn = AppKit.NSButton.buttonWithTitle_target_action_("Speichern", controller, "ok:")
    ok_btn.setKeyEquivalent_("\r")  # Enter = Default
    button_row = AppKit.NSStackView.alloc().init()
    button_row.setOrientation_(0)  # horizontal
    button_row.setSpacing_(10)
    spacer = AppKit.NSView.alloc().init()
    spacer.setContentHuggingPriority_forOrientation_(1, 0)  # dehnt sich
    button_row.addArrangedSubview_(spacer)
    button_row.addArrangedSubview_(cancel_btn)
    button_row.addArrangedSubview_(ok_btn)
    button_row.setTranslatesAutoresizingMaskIntoConstraints_(False)
    stack.addArrangedSubview_(button_row)
    pending.append(button_row.widthAnchor().constraintEqualToAnchor_(stack.widthAnchor()))

    style = AppKit.NSWindowStyleMaskTitled | AppKit.NSWindowStyleMaskClosable
    window = AppKit.NSWindow.alloc().initWithContentRect_styleMask_backing_defer_(
        NSMakeRect(0, 0, 520, 560), style, AppKit.NSBackingStoreBuffered, False)
    window.setTitle_(title)
    window.setReleasedWhenClosed_(False)
    controller._window = window

    content = window.contentView()
    content.addSubview_(stack)
    AppKit.NSLayoutConstraint.activateConstraints_([
        stack.leadingAnchor().constraintEqualToAnchor_constant_(content.leadingAnchor(), 16),
        stack.trailingAnchor().constraintEqualToAnchor_constant_(content.trailingAnchor(), -16),
        stack.topAnchor().constraintEqualToAnchor_constant_(content.topAnchor(), 16),
        stack.bottomAnchor().constraintEqualToAnchor_constant_(content.bottomAnchor(), -16),
    ] + pending)

    content.layoutSubtreeIfNeeded()
    fitting = stack.fittingSize()
    window.setContentSize_(NSMakeSize(max(500, fitting.width + 32), fitting.height + 32))
    if first_field is not None:
        window.setInitialFirstResponder_(first_field)

    app = AppKit.NSApplication.sharedApplication()
    app.activateIgnoringOtherApps_(True)
    window.center()
    window.makeKeyAndOrderFront_(None)
    response = app.runModalForWindow_(window)
    window.orderOut_(None)
    return response == _SETTINGS_OK