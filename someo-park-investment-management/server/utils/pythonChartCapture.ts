// E2B displays a PIL image whenever Image.save receives a file path. A
// matplotlib savefig(path) followed by show() therefore emits two resolutions
// of the same figure. Suppress that save-only display, not the file write.
// Keep exports as a fallback when the figure is closed without being shown.
const CHART_CAPTURE_SETUP = `
import matplotlib.figure as _sp_figure_module
from IPython import get_ipython as _sp_get_ipython
from IPython.utils.capture import capture_output as _sp_capture_output
import uuid as _sp_uuid

class _SomeoChartCapture:
    def __init__(self):
        self.pending = {}
        self.superseded = set()
        self.marker = '_someo_chart_' + _sp_uuid.uuid4().hex
        self.next_export = 0
        self.shell = _sp_get_ipython()
        self.savefig = _sp_figure_module.Figure.savefig
        self.format = self.shell.display_formatter.format
        self.capture = _sp_capture_output(stdout=False, stderr=False, display=True)
        self.outputs = self.capture.__enter__()
        guard = self

        def savefig(figure, *args, **kwargs):
            with _sp_capture_output(stdout=False, stderr=False, display=True) as captured:
                result = guard.savefig(figure, *args, **kwargs)
            if captured.outputs:
                # Figure objects, not reusable integer IDs, identify exports.
                # A new export after show() becomes pending again.
                guard.next_export += 1
                try:
                    # Render at the normal display settings after savefig has
                    # restored DPI. Compare exact content within this figure,
                    # so save(old) -> mutate -> show(new) retains both versions.
                    canonical = guard.format(figure)[0].get('image/png')
                except Exception:
                    canonical = None
                guard.pending[figure] = (guard.next_export, canonical)
                for output in captured.outputs:
                    metadata = dict(output.metadata or {})
                    metadata[guard.marker] = guard.next_export
                    output.metadata = metadata
                    output.display()
            return result

        def format(obj, *args, **kwargs):
            result = guard.format(obj, *args, **kwargs)
            if isinstance(obj, _sp_figure_module.Figure) and any(
                key.startswith('image/') for key in result[0]
            ):
                pending = guard.pending.pop(obj, None)
                if pending is not None:
                    export, canonical = pending
                    if canonical is not None and canonical == result[0].get('image/png'):
                        guard.superseded.add(export)
            return result

        _sp_figure_module.Figure.savefig = savefig
        self.shell.display_formatter.format = format

    def finish(self):
        try:
            # Nested run_cell normally flushes already. This also covers errors
            # and preserves implicit plots before restoring the output capture.
            from matplotlib_inline.backend_inline import flush_figures
            flush_figures()
        finally:
            _sp_figure_module.Figure.savefig = self.savefig
            self.shell.display_formatter.format = self.format
            self.capture.__exit__(None, None, None)
            self.pending.clear()
        # Retain original output order (including saved-and-closed figures).
        # Only the paired save side effect is removed; prior export versions,
        # unrelated figures, and standalone PIL displays remain untouched.
        for output in self.outputs.outputs:
            metadata = dict(output.metadata or {})
            export = metadata.pop(self.marker, None)
            if export not in self.superseded:
                output.metadata = metadata
                output.display()

_sp_chart_capture = _SomeoChartCapture()
`

export function buildPythonChartCode(code: string, preamble = ''): string {
  // A nested IPython cell preserves literal strings, magics, and display of
  // the final expression. Indenting source into try/finally would change them.
  return `${preamble}\n${CHART_CAPTURE_SETUP}\ntry:\n    _sp_chart_capture.shell.run_cell(${JSON.stringify(code)}, store_history=False).raise_error()\nfinally:\n    _sp_chart_capture.finish()\n`
}
