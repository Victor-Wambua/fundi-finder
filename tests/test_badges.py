from __future__ import annotations

from fundifinder.badges import BadgeKey, VerificationFacts, derive_badges, distinct_reference_count


def keys(**facts):
    return {b.key for b in derive_badges(VerificationFacts(**facts))}


def test_nothing_recorded_means_no_badges():
    assert keys() == set()


def test_phone_badge_needs_a_verified_phone():
    assert keys(phone_verified=True) == {BadgeKey.PHONE}


def test_id_badge_needs_an_approved_document():
    assert keys(id_document_approved=True) == {BadgeKey.ID}


def test_references_need_two_distinct_numbers():
    assert BadgeKey.REFERENCES not in keys(confirmed_reference_phones=("+254700000001",))
    assert BadgeKey.REFERENCES not in keys(confirmed_reference_phones=("+254700000001", "+254700000001"))
    assert BadgeKey.REFERENCES in keys(confirmed_reference_phones=("+254700000001", "+254700000002"))


def test_own_number_does_not_count_as_a_reference():
    facts = VerificationFacts(
        confirmed_reference_phones=("+254700000001", "+254700000002"), own_phone="+254700000002"
    )
    assert distinct_reference_count(facts) == 1
    assert BadgeKey.REFERENCES not in {b.key for b in derive_badges(facts)}


def test_skills_need_three_photos():
    assert BadgeKey.SKILLS not in keys(portfolio_photo_count=2)
    assert BadgeKey.SKILLS in keys(portfolio_photo_count=3)


def test_all_four_badges():
    assert len(keys(phone_verified=True, id_document_approved=True, portfolio_photo_count=5,
                    confirmed_reference_phones=("a", "b"))) == 4


def test_each_badge_explains_why_it_was_earned():
    badges = derive_badges(VerificationFacts(phone_verified=True, id_document_approved=True))
    assert all(b.earned_because for b in badges)
