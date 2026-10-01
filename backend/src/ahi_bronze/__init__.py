"""File -> Bronze ingestion rules (AHI scenario document), as pure functions.

Nothing here talks to a database: the planner decides, from cleaned tables plus what
the bronze layer already holds, which table each output goes to and how (create,
append, reorder, evolve, replace, new table, skip). The API service executes the plan
once a person has confirmed it.
"""
