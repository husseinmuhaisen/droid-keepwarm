"""droid-keepwarm: keep Factory Droid's prompt cache warm during breaks.

A keepwarm daemon that pings a fork of your session before the 5-minute
cache TTL lapses, plus a guard that warns before an expensive cold send.
"""

__version__ = "0.1.0"
