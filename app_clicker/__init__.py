"""App Clicker — an AI-driven manual tester for native Windows desktop apps and web apps.

It launches (or attaches to) a Windows application, reads its UI Automation
tree plus a screenshot, and lets Claude decide the next action to accomplish a
plain-English task or a list of test cases. Every step is logged to a report.
With ``--engine web`` it drives a web app in a browser through Playwright instead.
"""

__version__ = "0.1.0"
