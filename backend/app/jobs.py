"""Durable leased work queue. Business changes enqueue work in the same transaction."""

import uuid


def enqueue(conn, kind, object_id):
    conn.execute(
        "insert into work_jobs (kind, object_id) values (%s, %s) on conflict (kind, object_id) do nothing",
        (kind, object_id),
    )


def claim(conn):
    conn.execute("""update work_jobs set status = 'failed', last_error = 'LeaseExhausted', lease_until = null
                    where status = 'running' and lease_until < now() and attempts >= 8""")
    return conn.execute(
        """with candidate as (
            select id from work_jobs
            where attempts < 8 and ((status = 'pending' and available_at <= now())
                or (status = 'running' and lease_until < now()))
            order by available_at for update skip locked limit 1
        ) update work_jobs j set status = 'running', attempts = attempts + 1,
            lease_until = now() + interval '5 minutes', lease_token = %s
          from candidate c where j.id = c.id returning j.*""",
        (uuid.uuid4(),),
    ).fetchone()


def finish(conn, job, error=None):
    if error:
        # Persist an error category only; exception messages may contain document/account data.
        conn.execute(
            """update work_jobs set status = case when attempts >= 8 then 'failed' else 'pending' end,
               available_at = now() + make_interval(secs => least(3600, power(2, attempts)::int)),
               lease_until = null, last_error = %s
               where id = %s and lease_token = %s""",
            (type(error).__name__, job["id"], job["lease_token"]),
        )
    else:
        conn.execute("update work_jobs set status = 'done', lease_until = null, last_error = null where id = %s and lease_token = %s",
                     (job["id"], job["lease_token"]))
