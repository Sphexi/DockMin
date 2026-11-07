"""Tests for OnboardingReviewStore: who may use a held review, and one apply at a time."""

from app.services.onboarding_plan import OnboardingReview
from app.services.onboarding_store import OnboardingReviewStore

USER, OTHER_USER = 1, 2
ENVIRONMENT = 7
REVIEW = OnboardingReview(ENVIRONMENT, "remote", (), (), ())


def test_a_review_being_applied_refuses_a_second_apply_until_the_first_ends() -> None:
    store = OnboardingReviewStore()
    token = store.put(USER, ENVIRONMENT, REVIEW)

    first = store.begin_apply(token, USER, ENVIRONMENT)
    second = store.begin_apply(token, USER, ENVIRONMENT)
    store.end_apply(token)
    third = store.begin_apply(token, USER, ENVIRONMENT)

    assert (first, second, third) == (True, False, True)


def test_another_user_or_a_dropped_review_cannot_begin_an_apply() -> None:
    store = OnboardingReviewStore()
    token = store.put(USER, ENVIRONMENT, REVIEW)

    assert store.begin_apply(token, OTHER_USER, ENVIRONMENT) is False
    store.discard(token)
    assert store.begin_apply(token, USER, ENVIRONMENT) is False
    store.end_apply(token)
