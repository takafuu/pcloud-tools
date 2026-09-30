"""Saved evidence determines whether a version choice is meaningful."""

def category(review):
    if review.get('structural') or review.get('diagnostic'):
        return 'diagnostic'
    from .event_sync import ID_MISMATCH
    if review.get('reason') == ID_MISMATCH:
        return 'recheck'
    local, cloud = review.get('local'), review.get('cloud')
    if (isinstance(local, dict) and isinstance(cloud, dict)
            and (local.get('exists') or cloud.get('exists'))):
        return 'choice'
    return 'diagnostic'


def counts(reviews):
    result = {'review_count': 0, 'diagnostic_count': 0, 'recheck_count': 0}
    for review in reviews:
        kind = category(review)
        result[{'choice': 'review_count', 'diagnostic': 'diagnostic_count', 'recheck': 'recheck_count'}[kind]] += 1
    return result
