// WP-F, system and install config: this package's own file (index.html's package block).
// Its routes are cousin_lib/console/routes_system.py.
//
// The seams (ui.jsx): add UI here, never in a shared file.
//   registerView({ id: "system", label: "System", icon: I.host, order: 60, render: (props) => <SystemView {...props} /> });
//   Slots: inspector.lane, inspector.panels, inspector.actions (props
//   { cousin }), settings.panels (props { auth, setAuth }).
//   A view of its own: registerView({ id, label, icon, order, render }).
//   Write-only secrets: <SecretField status={...} onSubmit={async v => ...} />.
//   Long operations: useLongOp(slug) or <LongOpStatus slug={...} />.
// Publish what another file needs with Object.assign(window, { ... }).
