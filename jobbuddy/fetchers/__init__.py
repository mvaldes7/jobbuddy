"""Fetchers for PUBLIC employer ATS job-board APIs plus job-alert emails from Mike's own inbox.

No LinkedIn/Indeed page scraping and no Easy Apply automation: both sites' User Agreements
prohibit automated access/scraping and automated applying (see SAFETY.md).
"""
from jobbuddy.fetchers.base import CompanyTarget, JobPosting, load_companies  # noqa: F401
