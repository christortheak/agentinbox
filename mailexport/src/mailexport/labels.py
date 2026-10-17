"""launchd job labels, shared by the CLI and the worker."""
EXPORT_LABEL = "com.agentinbox.export"
FETCH_LABEL = "com.agentinbox.fetch"
# The settings page. Runs the plain Python entry point (never the launcher), so
# it holds no TCC grants; started only via `launchctl kickstart`.
CONFIGUI_LABEL = "com.agentinbox.configui"
