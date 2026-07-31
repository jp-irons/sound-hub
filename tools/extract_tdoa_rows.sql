-- extract_tdoa_rows.sql — pull just the rows needed for the offline TDOA
-- method comparison (tools/validate_toa_real_pulls.py) out of the live,
-- multi-GB sound_hub.db, into a small standalone extract db, instead of
-- copying the whole thing (2.7GB, almost certainly dominated by
-- trigger_events -- db.py's own comment notes it reached "18M+ rows /
-- 2.3GB" with no pruning -- tdoa_attempts/tdoa_attempt_nodes/node_positions
-- are all tiny by comparison).
--
-- Run ON THE HUB, against the real sound_hub.db:
--   sqlite3 sound_hub.db < extract_tdoa_rows.sql
-- Produces /tmp/sound_hub_extract.db (small — just the 3 tables below,
-- filtered to the attempts this comparison needs).
--
-- Covers:
--   attempt_17400 / attempt_17407 (known tdoa_attempts.id values, matching
--     the test/tdoa_pulls/ directory names).
--   attempt_pied_16-21 (no numeric ID in the directory name — instead
--     matched by t_start_us proximity to the 5 WAV files already
--     downloaded for it, which cluster within ~1s of each other:
--     1785219688031541 .. 1785219688758236).

ATTACH DATABASE '/tmp/sound_hub_extract.db' AS extract;

DROP TABLE IF EXISTS extract.tdoa_attempts;
CREATE TABLE extract.tdoa_attempts AS
SELECT * FROM main.tdoa_attempts
WHERE id IN (17400, 17407)
   OR t_start_us BETWEEN 1785219687000000 AND 1785219690000000;

DROP TABLE IF EXISTS extract.tdoa_attempt_nodes;
CREATE TABLE extract.tdoa_attempt_nodes AS
SELECT * FROM main.tdoa_attempt_nodes
WHERE attempt_id IN (SELECT id FROM extract.tdoa_attempts);

DROP TABLE IF EXISTS extract.node_positions;
CREATE TABLE extract.node_positions AS
SELECT * FROM main.node_positions;

DETACH DATABASE extract;

.print 'Extract written to /tmp/sound_hub_extract.db'
