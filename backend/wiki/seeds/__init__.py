"""Static seed data shipped with the wiki package.

The package ships a small read-only payload of geographic /
ontological seed data so that a fresh ZLAgent install can answer
basic geo queries without any external network call. The seeds
live next to the code (rather than under the gitignored ``data/``)
so they version-control alongside whatever code consumes them.

Currently only ``geo/`` lives here. Future seeds (e.g. an HSK
vocabulary list, a list of common Chinese surnames) would land as
their own subpackage with the same convention: one JSON file per
type, plus a tiny README explaining the source.
"""
