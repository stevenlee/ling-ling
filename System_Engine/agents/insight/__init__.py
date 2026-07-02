"""agents.insight — responsibility mixins of InsightAgent (P2f).

File-level decomposition: each mixin owns one concern; InsightAgent composes
them via inheritance so instance state, the test surface (__new__-built
skeletons, private-method calls) and behavior stay byte-identical.
Composition-style injection is P3 work.
"""
