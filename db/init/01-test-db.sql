-- Runs once, when the local Postgres volume is first created.
-- A second database for pytest, so tests never touch the dev data.
CREATE DATABASE triage_test;
