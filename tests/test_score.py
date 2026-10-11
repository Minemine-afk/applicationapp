from mcf_autoapply.config import Profile, Ranking
from mcf_autoapply.models import Job
from mcf_autoapply.pipeline.score import Ranker

RANKING = Ranking(entry_level_keywords=["fresh graduate", "junior", "graduate"],
                  entry_level_weight=0.6, skills_for_full_marks=4)
PROFILE = Profile(skills=["Python", "Java", "C++", "SQL", "Node.js"])


def rank(raw):
    return Ranker(PROFILE, RANKING).rank(Job.from_api(raw))


def test_fresh_grad_title_ranks_above_experienced(job_factory):
    fresh = rank(job_factory(title="Graduate Software Engineer", minimumYearsExperience=0))
    mid = rank(job_factory(title="Software Engineer", minimumYearsExperience=2))
    assert fresh.fresh_grad and not mid.fresh_grad
    assert fresh.score > mid.score
    assert fresh.entry_signals == ["title: graduate", "0 years required"]


def test_entry_level_position_counts_but_junior_executive_is_not_fresh(job_factory):
    entry = rank(job_factory(positionLevels=[{"position": "Fresh/entry level"}]))
    junior_exec = rank(job_factory(positionLevels=[{"position": "Junior Executive"}]))
    assert entry.fresh_grad and "level: fresh/entry" in entry.entry_signals
    assert not junior_exec.fresh_grad
    assert entry.entry_score > junior_exec.entry_score


def test_skills_matched_as_whole_words(job_factory):
    r = rank(job_factory(
        description="<p>We use JavaScript, C++ and Node.js with SQL.</p>", skills=[]))
    # "Java" must not match inside "JavaScript".
    assert r.matched_skills == ["C++", "Node.js", "SQL"]
    assert r.skills_score == 75
