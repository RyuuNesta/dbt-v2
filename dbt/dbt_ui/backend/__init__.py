"""
ASG dbt Studio - backend package.

Deliberately stdlib-only apart from what dbt-core already installs
(PyYAML, Jinja2, google-cloud-bigquery). Nothing here needs pip install,
which matters on locked-down corporate machines.
"""

__version__ = "1.0.0"
