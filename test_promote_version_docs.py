"""The promotion docs must not claim "code only" without explaining the bump.

`promote.sh` does TWO things with VERSION: it pins every tracked file to the
TARGET's value (so a version set on the source can never cross), and then
advances the TARGET's own counter by one step inside the promotion commit.

promote.yml's header used to say only the first half — "pins every tracked
VERSION file back to the target's value, so promotion carries CODE ONLY" — and
every reader, human or LLM reviewer, then hit a flat contradiction on opening
the diff and seeing `-1.35 / +1.36`. The skeptical panel duly rejected a run
of correct promotion PRs with "VERSION contradicts the description", and the
promotion queue stalled fleet-wide.

The behaviour was right; the description was wrong. These tests keep the two
halves together wherever "code only" is claimed.
"""
import pathlib
import re
import unittest

HERE = pathlib.Path(__file__).parent
PROMOTE_YML = HERE / ".github" / "workflows" / "promote.yml"
BACKMERGE_YML = HERE / ".github" / "workflows" / "backmerge.yml"
PROMOTE_SH = HERE / ".github" / "scripts" / "promote.sh"

# The behaviour the docs must not omit, however it happens to be worded.
_ADVANCE = re.compile(r"advanc\w*\s+(?:the\s+\S+\s+)?(?:own\s+)?(?:counter\s+)?one step"
                      r"|advanced one step"
                      r"|counter by exactly one step"
                      r"|own counter by one step", re.I)


class PromoteShIsTheSourceOfTruth(unittest.TestCase):
    def test_the_script_really_does_advance_the_target(self):
        """If this ever stops being true the docs below are the ones that are
        right and this test is the one that should be deleted."""
        src = PROMOTE_SH.read_text(encoding="utf-8")
        self.assertIn("Advance the TARGET's own version", src)
        self.assertIn("bump_version.py", src)

    def test_the_script_header_explains_both_halves(self):
        head = "\n".join(PROMOTE_SH.read_text(encoding="utf-8").splitlines()[:30])
        self.assertIn("pinned", head.lower())
        self.assertRegex(head, _ADVANCE)


class DocsExplainTheBump(unittest.TestCase):
    def _assert_pairs_up(self, path):
        text = path.read_text(encoding="utf-8")
        lowered = text.lower()
        self.assertIn("code only", lowered,
                      "%s no longer claims 'code only' — update this test" % path.name)
        self.assertRegex(
            text, _ADVANCE,
            "%s claims 'code only' but never says the target's own counter is "
            "advanced one step. That reads as 'VERSION does not change', which "
            "the diff then contradicts — exactly what stalled the queue." % path.name)

    def test_promote_yml(self):
        self._assert_pairs_up(PROMOTE_YML)

    def test_backmerge_yml(self):
        self._assert_pairs_up(BACKMERGE_YML)

    def test_promote_yml_header_warns_a_reviewer_directly(self):
        """The header is what a reviewer reads first; the clarification has to
        be THERE, not buried 400 lines down in the body generator."""
        head = "\n".join(PROMOTE_YML.read_text(encoding="utf-8").splitlines()[:40])
        self.assertRegex(head, _ADVANCE,
                         "the clarification is not in promote.yml's header comment")
        self.assertIn("does NOT mean the diff", head,
                      "the header should say explicitly what 'code only' does not mean")


class GeneratedPrBodiesExplainTheBump(unittest.TestCase):
    """The PR body is what the panel actually reads — the generators in both
    workflows must emit the pin AND the advance."""

    def _body_generator(self, path, anchor):
        text = path.read_text(encoding="utf-8")
        i = text.find(anchor)
        self.assertNotEqual(i, -1, "anchor %r not found in %s" % (anchor, path.name))
        return text[i:i + 1200]

    def test_promote_body_mentions_both(self):
        seg = self._body_generator(PROMOTE_YML, "Guardrails & Side-Effect Assessment")
        self.assertIn("pinned", seg)
        self.assertRegex(seg, _ADVANCE)

    def test_backmerge_body_mentions_both(self):
        seg = self._body_generator(BACKMERGE_YML, "Carries **code only**")
        self.assertIn("pinned", seg)
        self.assertRegex(seg, _ADVANCE)

    def test_backmerge_body_names_the_expectation(self):
        seg = self._body_generator(BACKMERGE_YML, "Carries **code only**")
        self.assertIn("expected", seg.lower(),
                      "the body should tell the reader a VERSION change is expected "
                      "rather than leaving them to infer it")


if __name__ == "__main__":
    unittest.main()
