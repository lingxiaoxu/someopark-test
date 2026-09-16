import assert from 'node:assert/strict'
import { spawnSync } from 'node:child_process'
import test from 'node:test'
import { buildPythonChartCode } from '../utils/pythonChartCapture.js'

const cases = [
  { name: 'save then show', count: 1, code: `plt.plot([1,2],[2,4]);plt.savefig('a.png',dpi=150);plt.show()` },
  { name: 'implicit display after save', count: 1, code: `plt.plot([1,2],[2,4]);plt.savefig('b.png',dpi=150)` },
  { name: 'save-only closed figure', count: 1, code: `plt.plot([1,2],[2,4]);plt.savefig('c.png');plt.close('all')` },
  { name: 'two distinct figures', count: 2, code: `for i in range(2):\n    fig, ax=plt.subplots();ax.set_title(str(i));ax.plot([1,2],[i,i+1]);fig.savefig('d%s.png'%i);plt.show()` },
  { name: 'changed figure after show', count: 2, code: `fig,ax=plt.subplots();ax.plot([1,2],[2,4]);plt.show();ax.plot([1,2],[4,2]);fig.savefig('e.png');plt.close(fig)` },
  { name: 'changed figure after save before show', count: 2, code: `fig,ax=plt.subplots();ax.plot([1,2],[2,4]);fig.savefig('changed.png',dpi=150);ax.plot([1,2],[4,2]);plt.show()` },
  { name: 'standalone PIL save', count: 1, code: `from PIL import Image\nImage.new('RGB',(20,20),'red').save('f.png')` },
  { name: 'PNG SVG PDF exports', count: 1, code: `fig,ax=plt.subplots();ax.plot([1,2],[3,4]);fig.savefig('g.svg');fig.savefig('g.pdf');fig.savefig('g.png');plt.show()` },
  { name: 'closed export before different displayed figure', count: 2, code: `fig,ax=plt.subplots();ax.plot([1,2],[3,4]);fig.savefig('h.png');plt.close(fig)\nfig,ax=plt.subplots();ax.plot([1,2],[4,3]);fig.savefig('i.png');plt.show()` },
  { name: 'two changed saved versions of one figure', count: 2, code: `fig,ax=plt.subplots();ax.plot([1,2],[3,4]);fig.savefig('j.png');ax.plot([1,2],[4,3]);fig.savefig('k.png');plt.close(fig)` },
  { name: 'source literals and implicit expression', count: 0, code: `value='''first\nsecond\nthird'''\nprint(repr(value))\n42` },
  { name: 'IPython magic preserved', count: 1, code: `%matplotlib inline\nplt.plot([1,2],[1,2]);plt.show()` },
  { name: 'user exception restores guard', count: 1, code: `plt.plot([1,2],[1,2]);plt.savefig('error.png');plt.close('all');raise ValueError('intentional-user-error')`, error: true },
  { name: 'subsequent run after error', count: 1, code: `plt.plot([1,2],[1,2]);plt.savefig('after.png');plt.show()` },
]

let results: any[] | undefined
function runCases() {
  if (results) return results
  // Reproduce the inspected E2B PIL hook using real IPython and Matplotlib.
  // No network or E2B credentials are required by these regression tests.
  const harness = `
import sys,json,os,tempfile,hashlib
from IPython.core.interactiveshell import InteractiveShell
from IPython.utils.capture import capture_output
from IPython.display import display
from PIL import Image
shell=InteractiveShell.instance()
shell.enable_gui=lambda gui:None
with capture_output():
    shell.run_cell("%matplotlib inline\\nimport matplotlib.pyplot as plt")
import matplotlib.pyplot as plt
import matplotlib.figure
original_pil_save=Image.Image.save
def e2b_save(image,fp,format=None,**options):
    if isinstance(fp,str): display(image)
    return original_pil_save(image,fp,format,**options)
Image.Image.save=e2b_save
original_figure_save=matplotlib.figure.Figure.savefig
original_format=shell.display_formatter.format
reports=[]
original_cwd=os.getcwd()
with tempfile.TemporaryDirectory() as folder:
    os.chdir(folder)
    for case in json.load(sys.stdin):
        plt.close('all')
        with capture_output() as captured:
            run=shell.run_cell(case['code'],store_history=False)
        pngs=[o for o in captured.outputs if 'image/png' in o.data]
        reports.append(dict(name=case['name'],success=run.success,count=len(pngs),
            types=[o.data.get('text/plain','') for o in pngs],
            hashes=[hashlib.sha256(o.data['image/png'].encode() if isinstance(o.data['image/png'],str) else o.data['image/png']).hexdigest() for o in pngs],
            stdout=captured.stdout,stderr=captured.stderr,
            texts=[o.data.get('text/plain','') for o in captured.outputs],
            marker_leaked=any(any(k.startswith('_someo_chart_') for k in (o.metadata or {})) for o in captured.outputs),
            restored=matplotlib.figure.Figure.savefig is original_figure_save and shell.display_formatter.format == original_format,
            files={p:os.path.getsize(p) for p in os.listdir('.') if os.path.isfile(p)}))
    os.chdir(original_cwd)
print(json.dumps(reports))
`
  const executed = spawnSync(process.env.SP_TEST_PYTHON || 'python3', ['-c', harness], {
    input: JSON.stringify(cases.map(c => ({ name: c.name, code: buildPythonChartCode(c.code) }))),
    encoding: 'utf8', timeout: 60_000, maxBuffer: 2_000_000,
  })
  assert.equal(executed.status, 0, executed.stderr || String(executed.error))
  results = JSON.parse(executed.stdout)
  return results!
}

for (const [index, fixture] of cases.entries()) {
  test(`Python chart capture: ${fixture.name}`, () => {
    const result = runCases()[index]
    assert.equal(result.success, !fixture.error, result.stdout + result.stderr)
    assert.equal(result.count, fixture.count)
    assert.equal(result.restored, true)
    assert.equal(result.marker_leaked, false)
    if (fixture.count === 2) assert.notEqual(result.hashes[0], result.hashes[1], 'distinct chart versions survive')
    if (fixture.name === 'closed export before different displayed figure') {
      assert.match(result.types[0], /PIL/)
      assert.match(result.types[1], /Figure/)
    }
    if (fixture.name === 'source literals and implicit expression') {
      assert.ok(result.stdout.includes("'first\\nsecond\\nthird'"))
      assert.ok(result.texts.includes('42'), JSON.stringify(result.texts))
    }
    if (fixture.name === 'PNG SVG PDF exports') {
      for (const path of ['g.png', 'g.svg', 'g.pdf']) assert.ok(result.files[path] > 0)
    }
  })
}
