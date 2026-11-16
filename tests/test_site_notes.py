"""Exercise the real field-note renderer in both asynchronous load orders."""
from pathlib import Path
import shutil
import subprocess
import unittest

ROOT = Path(__file__).resolve().parents[1]


@unittest.skipUnless(shutil.which("node"), "Node.js is needed to execute the browser renderer")
class NoteLoadingTests(unittest.TestCase):
    def test_both_store_arrival_orders(self):
        html = (ROOT / "site/index.html").read_text(encoding="utf-8")
        renderer = html.split("function noteEntry(n) {", 1)[1].split(
            "/* The field-notes issue form", 1
        )[0]
        renderer = "function noteEntry(n) {" + renderer
        script = r'''
const assert = require('node:assert/strict');
const elements = {'notes-list': {innerHTML: ''}, 'notes-count': {textContent: ''}};
const $ = id => elements[id];
const esc = value => String(value).replace(/[&<>"']/g, char =>
  ({'&':'&amp;', '<':'&lt;', '>':'&gt;', '"':'&quot;', "'":'&#39;'}[char]));
const t = key => key === 'notes_prompts' ? [] : key;
const catColor = slug => '#123456';
let LANG = 'en', CATS = [], NOTES = [], notesKey = null;
const note = {id:'issue-1', entry_url:'https://fonts.google.com/',
  title:'Example', body:'Example body', author:'creator', date:'2026-01-01',
  evidence:'https://github.com/skyzhao1223/free-for-creators/issues/1'};
const cats = [{slug:'fonts', entries:[{name:'Google Fonts', url:note.entry_url}]}];
'''
        script += renderer
        script += r'''
for (const notesFirst of [true, false]) {
  NOTES = []; CATS = []; notesKey = null;
  renderNotes();
  if (notesFirst) { NOTES = [note]; } else { CATS = cats; }
  renderNotes();
  if (notesFirst) { CATS = cats; } else { NOTES = [note]; }
  renderNotes();
  assert.ok(elements['notes-list'].innerHTML.includes('Google Fonts'));
  assert.ok(elements['notes-list'].innerHTML.includes('href="https://fonts.google.com/"'));
  assert.ok(elements['notes-list'].innerHTML.includes('background:#123456'));
  assert.equal(elements['notes-count'].textContent, '1');
  const rendered = elements['notes-list'].innerHTML;
  renderNotes();
  assert.equal(elements['notes-list'].innerHTML, rendered);
}
'''
        result = subprocess.run(
            [shutil.which("node"), "-e", script], capture_output=True, text=True, timeout=20
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
