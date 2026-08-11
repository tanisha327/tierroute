"""Macro-Planner + Model Director.

A frontier model is called ONCE to break a task into a tiered execution plan
(which model tier runs each step). The Director then steps through the plan,
swapping to the cheapest capable model per step, feeding each step only the
context it needs, executing real tools, and tracking baseline-vs-actual cost.
"""

__version__ = "0.1.0"
