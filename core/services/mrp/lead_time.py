"""Lead-time offsetting for MRP planned orders."""
import datetime


def release_date(receipt_date, lead_time_days):
    """Planned release date = planned receipt date - lead time (in days)."""
    days = int(lead_time_days or 0)
    return receipt_date - datetime.timedelta(days=days)
