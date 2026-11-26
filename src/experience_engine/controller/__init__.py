"""Adaptive strategy controller (M6): feasibility, estimators, calibration, policies, evaluation.

Pure package: no framework, database or network imports. Persistence lives behind
`ports/controller_store.py`. The controller can only choose among actions the existing
eligibility gate already admitted; nothing here can widen the action set.
"""
