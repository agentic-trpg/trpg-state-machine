BEGIN IMMEDIATE;
-- Projection starts AFTER an explicitly archived legacy prefix. These events are
-- not perceived=false or projection-complete: their original work/evidence stays.
CREATE TABLE legacy_perception_boundaries (
    session_id TEXT PRIMARY KEY REFERENCES sessions(session_id),
    through_event_seq INTEGER NOT NULL CHECK(through_event_seq > 0),
    reason TEXT NOT NULL CHECK(reason='legacy_event_evidence_unavailable'),
    FOREIGN KEY(session_id,through_event_seq) REFERENCES event_records(session_id,event_seq)
);
WITH classified AS (
    SELECT r.session_id,r.event_seq,
        CASE WHEN json_extract(e.evidence_json,'$.schema')='event-evidence/1'
            OR (r.source_command_id='legacy.unavailable'
                AND e.evidence_json IS NULL AND r.signals_json='[]'
                AND NOT EXISTS (SELECT 1 FROM command_receipts c
                    WHERE c.session_id=r.session_id AND c.command_id=r.source_command_id)
                AND NOT EXISTS (SELECT 1 FROM creation_commands c
                    WHERE c.session_id=r.session_id AND c.command_id=r.source_command_id))
            THEN 1 ELSE 0 END AS legacy
    FROM event_records r LEFT JOIN event_evidence e USING(session_id,event_seq)
), boundaries AS (
    SELECT session_id,
        coalesce(min(CASE WHEN legacy=0 THEN event_seq END)-1,max(event_seq)) AS cutoff
    FROM classified GROUP BY session_id
)
INSERT INTO legacy_perception_boundaries
SELECT session_id,cutoff,'legacy_event_evidence_unavailable' FROM boundaries WHERE cutoff>0;
-- Recompute only the perception cursor; genuine pending work above the boundary
-- still holds delivery. No decisions, Signals, Observations or completion invented.
INSERT INTO projection_cursors(session_id,consumer,event_seq)
SELECT b.session_id,'perception',coalesce(
    (SELECT min(w.event_seq)-1 FROM event_outbox w
     WHERE w.session_id=b.session_id AND w.consumer='perception'
       AND w.event_seq>b.through_event_seq AND w.done=0),s.event_seq)
FROM legacy_perception_boundaries b JOIN sessions s USING(session_id)
WHERE true
ON CONFLICT(session_id,consumer) DO UPDATE SET event_seq=excluded.event_seq;
PRAGMA user_version = 6;
COMMIT;
