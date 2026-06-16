#!/usr/bin/env python3
"""
MiniOB Vector Database Performance Benchmark

Systematic comparison experiments quantitatively analyzing the impact of:
  - Data scale on query latency and recall
  - Index parameters (lists, probes) on ANN search performance
  - Vector dimension on search efficiency
  - Performance comparison with general data type operations

Usage:
  python3 benchmark_runner.py --dim 128 --data-sizes "100,500,1000,5000,10000" \
      --lists "10,50,100,200" --probes "1,3,5,10,20" --k 10 --num-queries 50

Output: JSON file with all measurements, printed summary tables.
"""

import subprocess
import json
import time
import os
import sys
import argparse
import socket
import random
import math
from typing import List, Dict, Tuple, Optional

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
MINIOB_ROOT = os.path.join(SCRIPT_DIR, "../..")
OBSERVER_BIN = os.path.join(MINIOB_ROOT, "build_debug/bin/observer")
OBSERVER_INI = os.path.join(MINIOB_ROOT, "etc/observer.ini")
SOCKET_PATH = "/tmp/miniob_benchmark.sock"
DATA_DIR = os.path.join(MINIOB_ROOT, "db")

# ---------------------------------------------------------------------------
# Socket client for MiniOB
# ---------------------------------------------------------------------------

class MiniOBSocketClient:
    """Persistent Unix socket connection to MiniOB observer."""

    def __init__(self, socket_path: str, timeout: float = 30.0):
        self._path = socket_path
        self._timeout = timeout
        self._sock: Optional[socket.socket] = None

    def connect(self) -> bool:
        for _ in range(20):  # wait up to 10 seconds
            try:
                self._sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
                self._sock.settimeout(self._timeout)
                self._sock.connect(self._path)
                return True
            except (socket.error, FileNotFoundError):
                time.sleep(0.5)
        return False

    def execute(self, sql: str) -> Tuple[bool, str, float]:
        """Execute SQL, return (success, output, latency_ms)."""
        if self._sock is None:
            return False, "not connected", 0.0

        t0 = time.perf_counter()
        try:
            self._sock.sendall(sql.encode() + b'\x00')
            result = b''
            while True:
                data = self._sock.recv(8192)
                if not data:
                    break
                result += data
                if data[-1] == 0:
                    break
            elapsed = (time.perf_counter() - t0) * 1000
            return True, result.decode('utf-8').rstrip('\x00').strip(), elapsed
        except Exception as e:
            elapsed = (time.perf_counter() - t0) * 1000
            return False, str(e), elapsed

    def execute_batch(self, sqls: List[str]) -> List[Tuple[bool, str, float]]:
        results = []
        for sql in sqls:
            results.append(self.execute(sql))
        return results

    def close(self):
        if self._sock:
            self._sock.close()
            self._sock = None


# ---------------------------------------------------------------------------
# Observer lifecycle
# ---------------------------------------------------------------------------

def start_observer() -> Optional[subprocess.Popen]:
    """Start observer process. Returns Popen handle or None."""
    # Clean up stale data
    db_sys = os.path.join(DATA_DIR, "db", "sys")
    if os.path.exists(db_sys):
        import shutil
        shutil.rmtree(db_sys, ignore_errors=True)

    # Remove stale socket
    if os.path.exists(SOCKET_PATH):
        os.unlink(SOCKET_PATH)

    try:
        proc = subprocess.Popen(
            [OBSERVER_BIN, "-f", OBSERVER_INI,
             "-s", SOCKET_PATH, "-P", "unix"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            cwd=MINIOB_ROOT
        )
        time.sleep(1.5)
        # Check it didn't crash immediately
        if proc.poll() is not None:
            print("ERROR: Observer exited immediately with code", proc.returncode,
                  file=sys.stderr)
            return None
        return proc
    except FileNotFoundError:
        print(f"ERROR: Observer binary not found at {OBSERVER_BIN}", file=sys.stderr)
        print("Build with: cd miniob && mkdir -p build_debug && cd build_debug && cmake .. && make -j",
              file=sys.stderr)
        return None


def stop_observer(proc: Optional[subprocess.Popen]):
    """Stop observer and clean up."""
    if proc is None:
        return
    try:
        proc.terminate()
        proc.wait(timeout=10)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait(timeout=5)
    except Exception:
        pass

    # Clean up data
    db_sys = os.path.join(DATA_DIR, "db", "sys")
    if os.path.exists(db_sys):
        import shutil
        shutil.rmtree(db_sys, ignore_errors=True)
    if os.path.exists(SOCKET_PATH):
        os.unlink(SOCKET_PATH)


# ---------------------------------------------------------------------------
# Vector data generation
# ---------------------------------------------------------------------------

def generate_vector(dim: int, seed: int) -> List[float]:
    """Generate a random vector with deterministic seed."""
    rng = random.Random(seed)
    return [round(rng.uniform(-10.0, 10.0), 4) for _ in range(dim)]


def generate_query_vectors(dim: int, num_queries: int, base_seed: int = 999999) -> List[List[float]]:
    """Generate query vectors (different distribution shift from data)."""
    queries = []
    for i in range(num_queries):
        rng = random.Random(base_seed + i * 100)
        # Query vectors use slightly different range to test generalization
        queries.append([round(rng.uniform(-8.0, 8.0), 4) for _ in range(dim)])
    return queries


def format_vector_sql(vec: List[float]) -> str:
    """Format vector as SQL string_to_vector argument."""
    return "[" + ",".join(str(v) for v in vec) + "]"


# ---------------------------------------------------------------------------
# Core benchmark experiments
# ---------------------------------------------------------------------------

def run_setup_sql(client: MiniOBSocketClient, table_name: str,
                  dim: int, count: int) -> Tuple[float, float]:
    """Create table and insert data. Returns (setup_time_ms, insert_throughput)."""
    # Drop old table if exists
    client.execute(f"drop table {table_name};")

    # Create table
    ok, out, lat_create = client.execute(
        f"create table {table_name}(id int, embedding vector({dim}));")
    if not ok:
        print(f"  ERROR creating table: {out}", file=sys.stderr)
        return 0.0, 0.0

    # Insert rows in batches of 100 for efficiency
    insert_start = time.perf_counter()
    batch_size = 100
    success_count = 0

    for start in range(1, count + 1, batch_size):
        end = min(start + batch_size - 1, count)
        batch_sqls = []
        for i in range(start, end + 1):
            vec = generate_vector(dim, i)
            vec_str = format_vector_sql(vec)
            batch_sqls.append(
                f"insert into {table_name} values({i}, string_to_vector('{vec_str}'));")
        # Execute batch sequentially (MiniOB doesn't support multi-statement)
        for sql in batch_sqls:
            ok, out, lat = client.execute(sql)
            if ok:
                success_count += 1

    insert_total_ms = (time.perf_counter() - insert_start) * 1000
    setup_time = lat_create + insert_total_ms
    throughput = success_count / (insert_total_ms / 1000) if insert_total_ms > 0 else 0
    return setup_time, throughput


def exact_search(client: MiniOBSocketClient, table_name: str,
                 query_vec: List[float], k: int) -> Tuple[List[int], float]:
    """Run exact search (full table scan + sort). Returns (result_ids, latency_ms)."""
    vec_str = format_vector_sql(query_vec)
    sql = (f"select id from {table_name} "
           f"order by DISTANCE(embedding, string_to_vector('{vec_str}'), 'EUCLIDEAN') "
           f"limit {k};")
    ok, out, lat = client.execute(sql)

    if not ok:
        return [], lat

    ids = _parse_id_list(out)
    return ids, lat


def ann_search(client: MiniOBSocketClient, table_name: str,
               query_vec: List[float], k: int) -> Tuple[List[int], float]:
    """Run ANN search (uses vector index when available). Returns (result_ids, latency_ms)."""
    vec_str = format_vector_sql(query_vec)
    # Use L2 alias to ensure ANN path is taken when index exists
    sql = (f"select id from {table_name} "
           f"order by DISTANCE(embedding, string_to_vector('{vec_str}'), 'L2') "
           f"limit {k};")
    ok, out, lat = client.execute(sql)

    if not ok:
        return [], lat

    ids = _parse_id_list(out)
    return ids, lat


def _parse_id_list(output: str) -> List[int]:
    """Parse 'id\n1\n2\n3' output into list of ints."""
    lines = output.strip().split('\n')
    if not lines:
        return []
    # Check for error responses
    first_line = lines[0].strip()
    if first_line != 'id':
        if first_line in ('FAILURE', 'ERROR') or first_line.startswith('FAIL'):
            print(f"    [WARN] Query returned error: {first_line}", file=sys.stderr)
        return []
    ids = []
    for line in lines[1:]:
        line = line.strip()
        if line and line.isdigit():
            ids.append(int(line))
        elif line:
            # Might be non-numeric or empty
            try:
                ids.append(int(line))
            except ValueError:
                pass
    return ids


def compute_recall(approx_ids: List[int], exact_ids: List[int], k: int) -> float:
    """Compute recall@k."""
    if not exact_ids or k == 0:
        return 0.0
    exact_set = set(exact_ids[:k])
    approx_set = set(approx_ids[:k])
    return len(exact_set & approx_set) / min(k, len(exact_set))


def create_vector_index(client: MiniOBSocketClient, table_name: str,
                        idx_name: str, col_name: str,
                        lists: int, probes: int) -> float:
    """Create vector index. Returns build_time_ms."""
    sql = (f"CREATE VECTOR INDEX {idx_name} ON {table_name}({col_name}) "
           f"WITH (lists={lists}, probes={probes});")
    ok, out, lat = client.execute(sql)
    if not ok:
        print(f"  ERROR creating index: {out}", file=sys.stderr)
    return lat


def drop_index(client: MiniOBSocketClient, idx_name: str) -> float:
    """Drop index. Returns latency_ms."""
    ok, out, lat = client.execute(f"drop index {idx_name};")
    return lat


# ---------------------------------------------------------------------------
# Experiment 1: Data Scale vs Query Performance
# ---------------------------------------------------------------------------

def experiment_data_scale(client: MiniOBSocketClient, args) -> Dict:
    """Test exact vs ANN search across different data sizes."""
    print("\n" + "=" * 70)
    print("Experiment 1: Data Scale vs Query Performance")
    print("=" * 70)
    print(f"  dim={args.dim}, lists={args.lists_default}, probes={args.probes_default}, k={args.k}")

    sizes = [int(s.strip()) for s in args.data_sizes.split(",")]
    queries = generate_query_vectors(args.dim, args.num_queries)
    results = []

    for size in sizes:
        print(f"\n  --- Data size: {size} ---")
        table = f"bench_scale_{size}"

        # Setup
        setup_time, insert_rate = run_setup_sql(client, table, args.dim, size)
        print(f"  Setup: {setup_time:.0f}ms, Insert rate: {insert_rate:.0f} rows/s")

        # Exact search BEFORE index (forces true full scan)
        exact_lats = []
        exact_ground_truth_all = []  # save all exact results for recall
        for qvec in queries[:args.num_queries]:
            ids, lat = exact_search(client, table, qvec, args.k)
            exact_lats.append(lat)
            exact_ground_truth_all.append(ids)

        avg_exact = sum(exact_lats) / len(exact_lats) if exact_lats else 0

        # Create index
        idx_name = f"idx_scale_{size}"
        build_time = create_vector_index(client, table, idx_name, "embedding",
                                         args.lists_default, args.probes_default)
        print(f"  Index build: {build_time:.0f}ms")

        # ANN search (index exists, optimizer uses ANN path)
        ann_lats = []
        recalls = []
        for qi, qvec in enumerate(queries[:args.num_queries]):
            ann_ids, lat = ann_search(client, table, qvec, args.k)
            ann_lats.append(lat)
            recalls.append(compute_recall(ann_ids, exact_ground_truth_all[qi], args.k))

        avg_ann = sum(ann_lats) / len(ann_lats) if ann_lats else 0
        avg_recall = sum(recalls) / len(recalls) if recalls else 0
        speedup = avg_exact / avg_ann if avg_ann > 0 else 0

        print(f"  Exact latency: {avg_exact:.2f}ms")
        print(f"  ANN latency:   {avg_ann:.2f}ms")
        print(f"  Speedup:       {speedup:.2f}x")
        print(f"  Recall@{args.k}:    {avg_recall:.4f}")

        results.append({
            "data_size": size,
            "vector_dim": args.dim,
            "lists": args.lists_default,
            "probes": args.probes_default,
            "k": args.k,
            "setup_time_ms": round(setup_time, 2),
            "insert_rate_rows_per_sec": round(insert_rate, 1),
            "index_build_time_ms": round(build_time, 2),
            "exact_latency_ms_avg": round(avg_exact, 3),
            "exact_latency_ms_p50": round(_percentile(exact_lats, 50), 3),
            "exact_latency_ms_p99": round(_percentile(exact_lats, 99), 3),
            "ann_latency_ms_avg": round(avg_ann, 3),
            "ann_latency_ms_p50": round(_percentile(ann_lats, 50), 3),
            "ann_latency_ms_p99": round(_percentile(ann_lats, 99), 3),
            "speedup_ratio": round(speedup, 2),
            "recall_avg": round(avg_recall, 4),
            "num_queries": args.num_queries,
        })

        # Cleanup for next iteration
        drop_index(client, idx_name)
        client.execute(f"drop table {table};")

    return {"experiment": "data_scale", "results": results}


# ---------------------------------------------------------------------------
# Experiment 2: lists Parameter Sensitivity
# ---------------------------------------------------------------------------

def experiment_lists(client: MiniOBSocketClient, args) -> Dict:
    """Test effect of lists parameter on ANN performance."""
    print("\n" + "=" * 70)
    print("Experiment 2: lists Parameter Sensitivity")
    print("=" * 70)
    print(f"  dim={args.dim}, N={args.lists_data_size}, probes={args.probes_default}, k={args.k}")

    table = "bench_lists"
    lists_values = [int(s.strip()) for s in args.lists.split(",")]
    queries = generate_query_vectors(args.dim, args.num_queries)
    results = []

    # Setup data once
    setup_time, insert_rate = run_setup_sql(client, table, args.dim, args.lists_data_size)
    print(f"  Setup: {setup_time:.0f}ms, Insert rate: {insert_rate:.0f} rows/s")

    # Pre-compute exact ground truth (no index yet → true full scan)
    exact_ground_truth_all = []
    for qvec in queries[:args.num_queries]:
        ids, _ = exact_search(client, table, qvec, args.k)
        exact_ground_truth_all.append(ids)

    for lv in lists_values:
        print(f"\n  --- lists={lv} ---")
        idx_name = f"idx_lists_{lv}"
        build_time = create_vector_index(client, table, idx_name, "embedding",
                                         lv, args.probes_default)
        print(f"  Build time: {build_time:.0f}ms")

        ann_lats = []
        recalls = []
        for qi, qvec in enumerate(queries[:args.num_queries]):
            ann_ids, lat = ann_search(client, table, qvec, args.k)
            ann_lats.append(lat)
            recalls.append(compute_recall(ann_ids, exact_ground_truth_all[qi], args.k))

        avg_ann = sum(ann_lats) / len(ann_lats) if ann_lats else 0
        avg_recall = sum(recalls) / len(recalls) if recalls else 0

        print(f"  ANN latency: {avg_ann:.2f}ms, Recall@{args.k}: {avg_recall:.4f}")

        results.append({
            "lists": lv,
            "probes": args.probes_default,
            "data_size": args.lists_data_size,
            "vector_dim": args.dim,
            "index_build_time_ms": round(build_time, 2),
            "ann_latency_ms_avg": round(avg_ann, 3),
            "recall_avg": round(avg_recall, 4),
        })

        drop_index(client, idx_name)

    client.execute(f"drop table {table};")
    return {"experiment": "lists_sensitivity", "results": results}


# ---------------------------------------------------------------------------
# Experiment 3: probes Parameter Sensitivity
# ---------------------------------------------------------------------------

def experiment_probes(client: MiniOBSocketClient, args) -> Dict:
    """Test effect of probes parameter on ANN performance."""
    print("\n" + "=" * 70)
    print("Experiment 3: probes Parameter Sensitivity")
    print("=" * 70)
    print(f"  dim={args.dim}, N={args.probes_data_size}, lists={args.lists_default}, k={args.k}")

    table = "bench_probes"
    probes_values = [int(s.strip()) for s in args.probes.split(",")]
    queries = generate_query_vectors(args.dim, args.num_queries)
    results = []

    # Setup data and index once (with fixed lists)
    setup_time, insert_rate = run_setup_sql(client, table, args.dim, args.probes_data_size)
    print(f"  Setup: {setup_time:.0f}ms, Insert rate: {insert_rate:.0f} rows/s")

    # Pre-compute exact ground truth (no index yet → true full scan)
    exact_ground_truth_all = []
    for qvec in queries[:args.num_queries]:
        ids, _ = exact_search(client, table, qvec, args.k)
        exact_ground_truth_all.append(ids)

    # We need to recreate index for each probes value
    for pv in probes_values:
        if pv > args.lists_default:
            print(f"  --- probes={pv} (skipped: probes > lists) ---")
            continue

        print(f"\n  --- probes={pv} ---")
        idx_name = f"idx_probes_{pv}"
        build_time = create_vector_index(client, table, idx_name, "embedding",
                                         args.lists_default, pv)
        print(f"  Build time: {build_time:.0f}ms")

        ann_lats = []
        recalls = []
        for qi, qvec in enumerate(queries[:args.num_queries]):
            ann_ids, lat = ann_search(client, table, qvec, args.k)
            ann_lats.append(lat)
            recalls.append(compute_recall(ann_ids, exact_ground_truth_all[qi], args.k))

        avg_ann = sum(ann_lats) / len(ann_lats) if ann_lats else 0
        avg_recall = sum(recalls) / len(recalls) if recalls else 0

        print(f"  ANN latency: {avg_ann:.2f}ms, Recall@{args.k}: {avg_recall:.4f}")

        results.append({
            "lists": args.lists_default,
            "probes": pv,
            "data_size": args.probes_data_size,
            "vector_dim": args.dim,
            "index_build_time_ms": round(build_time, 2),
            "ann_latency_ms_avg": round(avg_ann, 3),
            "recall_avg": round(avg_recall, 4),
        })

        drop_index(client, idx_name)

    client.execute(f"drop table {table};")
    return {"experiment": "probes_sensitivity", "results": results}


# ---------------------------------------------------------------------------
# Experiment 4: Vector Dimension vs Performance
# ---------------------------------------------------------------------------

def experiment_dimension(client: MiniOBSocketClient, args) -> Dict:
    """Test effect of vector dimension on search performance."""
    print("\n" + "=" * 70)
    print("Experiment 4: Vector Dimension vs Performance")
    print("=" * 70)

    dims = [int(s.strip()) for s in args.dims.split(",")]
    queries_by_dim = {d: generate_query_vectors(d, args.num_queries) for d in dims}
    results = []

    for dim in dims:
        print(f"\n  --- dim={dim} ---")
        table = f"bench_dim_{dim}"

        setup_time, insert_rate = run_setup_sql(client, table, dim, args.dim_data_size)
        print(f"  Setup: {setup_time:.0f}ms, Insert rate: {insert_rate:.0f} rows/s")

        queries = queries_by_dim[dim]

        # Exact search
        exact_lats = []
        for qvec in queries[:args.num_queries]:
            ids, lat = exact_search(client, table, qvec, args.k)
            exact_lats.append(lat)

        avg_exact = sum(exact_lats) / len(exact_lats) if exact_lats else 0

        # Adjust lists for dimension (more dims need more lists for good partitioning)
        dim_adjusted_lists = max(10, int(args.lists_default * (dim / 128.0)))
        dim_adjusted_probes = args.probes_default

        idx_name = f"idx_dim_{dim}"
        build_time = create_vector_index(client, table, idx_name, "embedding",
                                         dim_adjusted_lists, dim_adjusted_probes)
        print(f"  Index build (lists={dim_adjusted_lists}): {build_time:.0f}ms")

        # Save exact ground truth before index
        exact_ground_truth_all = []
        for qvec in queries[:args.num_queries]:
            ids, _ = exact_search(client, table, qvec, args.k)
            exact_ground_truth_all.append(ids)

        # ANN search
        ann_lats = []
        recalls = []
        for qi, qvec in enumerate(queries[:args.num_queries]):
            ann_ids, lat = ann_search(client, table, qvec, args.k)
            ann_lats.append(lat)
            recalls.append(compute_recall(ann_ids, exact_ground_truth_all[qi], args.k))

        avg_ann = sum(ann_lats) / len(ann_lats) if ann_lats else 0
        avg_recall = sum(recalls) / len(recalls) if recalls else 0
        speedup = avg_exact / avg_ann if avg_ann > 0 else 0

        print(f"  Exact latency: {avg_exact:.2f}ms")
        print(f"  ANN latency:   {avg_ann:.2f}ms")
        print(f"  Speedup:       {speedup:.2f}x")
        print(f"  Recall@{args.k}:    {avg_recall:.4f}")

        results.append({
            "vector_dim": dim,
            "data_size": args.dim_data_size,
            "lists": dim_adjusted_lists,
            "probes": dim_adjusted_probes,
            "k": args.k,
            "index_build_time_ms": round(build_time, 2),
            "exact_latency_ms_avg": round(avg_exact, 3),
            "ann_latency_ms_avg": round(avg_ann, 3),
            "speedup_ratio": round(speedup, 2),
            "recall_avg": round(avg_recall, 4),
        })

        drop_index(client, idx_name)
        client.execute(f"drop table {table};")

    return {"experiment": "dimension", "results": results}


# ---------------------------------------------------------------------------
# Experiment 5: General Data Type Operations Comparison
# ---------------------------------------------------------------------------

def experiment_general_ops(client: MiniOBSocketClient, args) -> Dict:
    """Compare vector search with general data type operations."""
    print("\n" + "=" * 70)
    print("Experiment 5: General Data Type Operations Comparison")
    print("=" * 70)
    print(f"  N={args.general_data_size}, dim={args.dim}, k={args.k}")

    results = []
    N = args.general_data_size

    # --- Integer point query ---
    print("\n  --- Integer Point Query ---")
    table_int = "bench_int_ops"
    client.execute(f"drop table {table_int};")
    client.execute(f"create table {table_int}(id int, val int);")

    t0 = time.perf_counter()
    for i in range(1, N + 1):
        client.execute(f"insert into {table_int} values({i}, {i * 7 % 1000});")
    int_insert_ms = (time.perf_counter() - t0) * 1000

    # Create btree index
    client.execute(f"CREATE INDEX idx_int_id ON {table_int}(id);")

    # Point query latency
    pt_lats = []
    for i in range(args.num_queries):
        target = random.randint(1, N)
        ok, out, lat = client.execute(f"select * from {table_int} where id = {target};")
        pt_lats.append(lat)
    avg_pt = sum(pt_lats) / len(pt_lats) if pt_lats else 0
    print(f"  Point query (btree): {avg_pt:.3f}ms avg")

    # --- Integer range scan ---
    print("  --- Integer Range Scan ---")
    range_width = min(500, N // 2)
    range_lats = []
    for i in range(args.num_queries):
        lo = random.randint(1, N - range_width) if N > range_width else 1
        hi = lo + range_width
        ok, out, lat = client.execute(
            f"select * from {table_int} where val >= {lo} and val <= {hi};")
        range_lats.append(lat)
    avg_range = sum(range_lats) / len(range_lats) if range_lats else 0
    print(f"  Range scan ({range_width} rows): {avg_range:.3f}ms avg")

    # --- Integer sort + limit ---
    print("  --- Integer Sort + Limit ---")
    sort_lats = []
    for i in range(args.num_queries):
        ok, out, lat = client.execute(
            f"select id from {table_int} order by val limit {args.k};")
        sort_lats.append(lat)
    avg_sort = sum(sort_lats) / len(sort_lats) if sort_lats else 0
    print(f"  Sort+limit top-{args.k}: {avg_sort:.3f}ms avg")

    # --- Full table scan ---
    print("  --- Full Table Scan ---")
    scan_lats = []
    for i in range(min(args.num_queries, 10)):  # fewer iterations for expensive op
        ok, out, lat = client.execute(f"select count(*) from {table_int};")
        scan_lats.append(lat)
    avg_scan = sum(scan_lats) / len(scan_lats) if scan_lats else 0
    print(f"  Full scan count(*): {avg_scan:.3f}ms avg")

    # --- Vector exact search (for comparison) ---
    print("  --- Vector Exact Search ---")
    table_vec = "bench_vec_ops"
    run_setup_sql(client, table_vec, args.dim, N)
    queries = generate_query_vectors(args.dim, args.num_queries)
    vec_exact_lats = []
    exact_ground_truth_all = []
    for qvec in queries[:args.num_queries]:
        ids, lat = exact_search(client, table_vec, qvec, args.k)
        vec_exact_lats.append(lat)
        exact_ground_truth_all.append(ids)
    avg_vec_exact = sum(vec_exact_lats) / len(vec_exact_lats) if vec_exact_lats else 0
    print(f"  Vector exact top-{args.k}: {avg_vec_exact:.3f}ms avg")

    # --- Vector ANN search ---
    print("  --- Vector ANN Search ---")
    create_vector_index(client, table_vec, "idx_vec_ops", "embedding",
                        args.lists_default, args.probes_default)
    vec_ann_lats = []
    vec_recalls = []
    for qi, qvec in enumerate(queries[:args.num_queries]):
        ann_ids, lat = ann_search(client, table_vec, qvec, args.k)
        vec_ann_lats.append(lat)
        vec_recalls.append(compute_recall(ann_ids, exact_ground_truth_all[qi], args.k))
    avg_vec_ann = sum(vec_ann_lats) / len(vec_ann_lats) if vec_ann_lats else 0
    avg_vec_recall = sum(vec_recalls) / len(vec_recalls) if vec_recalls else 0
    print(f"  Vector ANN top-{args.k}: {avg_vec_ann:.3f}ms avg, recall={avg_vec_recall:.4f}")

    # Cleanup
    client.execute(f"drop table {table_int};")
    client.execute(f"drop table {table_vec};")

    return {
        "experiment": "general_ops_comparison",
        "data_size": N,
        "num_queries": args.num_queries,
        "results": {
            "integer_point_query_ms": round(avg_pt, 3),
            "integer_range_scan_500rows_ms": round(avg_range, 3),
            "integer_sort_limit_ms": round(avg_sort, 3),
            "integer_full_scan_count_ms": round(avg_scan, 3),
            "vector_exact_topk_ms": round(avg_vec_exact, 3),
            "vector_ann_topk_ms": round(avg_vec_ann, 3),
            "vector_ann_recall": round(avg_vec_recall, 4),
            "vector_dim": args.dim,
            "vector_lists": args.lists_default,
            "vector_probes": args.probes_default,
            "top_k": args.k,
        }
    }


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _percentile(data: List[float], p: float) -> float:
    """Compute percentile of sorted data."""
    if not data:
        return 0.0
    sorted_data = sorted(data)
    k = (len(sorted_data) - 1) * p / 100.0
    f = int(k)
    c = k - f
    if f + 1 < len(sorted_data):
        return sorted_data[f] * (1 - c) + sorted_data[f + 1] * c
    return sorted_data[f]


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(
        description="MiniOB Vector Database Performance Benchmark",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  # Quick test with small data
  python3 benchmark_runner.py --data-sizes "100,500,1000" --num-queries 10

  # Full benchmark
  python3 benchmark_runner.py --dim 128 --data-sizes "100,500,1000,5000,10000" \\
      --lists "10,50,100,200,500" --probes "1,3,5,10,20,50" \\
      --k 10 --num-queries 50 --output results.json

  # Skip certain experiments
  python3 benchmark_runner.py --skip-dimension --skip-general
        """)

    # Vector parameters
    parser.add_argument("--dim", type=int, default=128,
                        help="Vector dimension (default: 128)")
    parser.add_argument("--data-sizes", type=str, default="100,500,1000,5000,10000",
                        help="Data sizes for experiment 1 (default: 100,500,1000,5000,10000)")
    parser.add_argument("--lists", type=str, default="10,50,100,200,500",
                        help="lists values for experiment 2 (default: 10,50,100,200,500)")
    parser.add_argument("--probes", type=str, default="1,3,5,10,20,50",
                        help="probes values for experiment 3 (default: 1,3,5,10,20,50)")
    parser.add_argument("--dims", type=str, default="32,64,128,256,512",
                        help="Dimension values for experiment 4 (default: 32,64,128,256,512)")
    parser.add_argument("--k", type=int, default=10,
                        help="Top-K for queries (default: 10)")
    parser.add_argument("--num-queries", type=int, default=50,
                        help="Number of queries per test point (default: 50)")

    # Default index params
    parser.add_argument("--lists-default", type=int, default=100,
                        help="Default lists for experiments 1,3,4,5 (default: 100)")
    parser.add_argument("--probes-default", type=int, default=10,
                        help="Default probes for experiments 1,2,4,5 (default: 10)")

    # Data sizes for specific experiments
    parser.add_argument("--lists-data-size", type=int, default=10000,
                        help="Data size for lists experiment (default: 10000)")
    parser.add_argument("--probes-data-size", type=int, default=10000,
                        help="Data size for probes experiment (default: 10000)")
    parser.add_argument("--dim-data-size", type=int, default=5000,
                        help="Data size for dimension experiment (default: 5000)")
    parser.add_argument("--general-data-size", type=int, default=10000,
                        help="Data size for general ops experiment (default: 10000)")

    # Skip flags
    parser.add_argument("--skip-scale", action="store_true",
                        help="Skip experiment 1 (data scale)")
    parser.add_argument("--skip-lists", action="store_true",
                        help="Skip experiment 2 (lists sensitivity)")
    parser.add_argument("--skip-probes", action="store_true",
                        help="Skip experiment 3 (probes sensitivity)")
    parser.add_argument("--skip-dimension", action="store_true",
                        help="Skip experiment 4 (dimension)")
    parser.add_argument("--skip-general", action="store_true",
                        help="Skip experiment 5 (general ops)")

    # Output
    parser.add_argument("--output", "-o", type=str, default="benchmark_results.json",
                        help="Output JSON file path (default: benchmark_results.json)")

    args = parser.parse_args()

    # Validate paths
    if not os.path.exists(OBSERVER_BIN):
        print(f"ERROR: Observer binary not found: {OBSERVER_BIN}", file=sys.stderr)
        print("Build with: cd miniob && mkdir -p build_debug && cd build_debug && cmake .. && make -j",
              file=sys.stderr)
        sys.exit(1)

    report = {
        "metadata": {
            "test_date": time.strftime("%Y-%m-%d %H:%M:%S"),
            "observer_binary": OBSERVER_BIN,
            "config": {
                "vector_dim": args.dim,
                "data_sizes": args.data_sizes,
                "lists_range": args.lists,
                "probes_range": args.probes,
                "dims_range": args.dims,
                "k": args.k,
                "num_queries": args.num_queries,
                "lists_default": args.lists_default,
                "probes_default": args.probes_default,
            },
            "description": "MiniOB vector database systematic performance benchmark"
        },
        "experiments": {}
    }

    # Start observer
    print("Starting MiniOB observer...")
    observer_proc = start_observer()
    if observer_proc is None:
        print("ERROR: Failed to start observer.", file=sys.stderr)
        sys.exit(1)

    try:
        # Connect
        client = MiniOBSocketClient(SOCKET_PATH, timeout=600.0)
        if not client.connect():
            print("ERROR: Failed to connect to observer.", file=sys.stderr)
            stop_observer(observer_proc)
            sys.exit(1)

        print("Connected to observer.\n")

        # Warm up
        client.execute("select 1;")

        # --- Experiment 1: Data Scale ---
        if not args.skip_scale:
            report["experiments"]["data_scale"] = experiment_data_scale(client, args)

        # --- Experiment 2: lists ---
        if not args.skip_lists:
            report["experiments"]["lists_sensitivity"] = experiment_lists(client, args)

        # --- Experiment 3: probes ---
        if not args.skip_probes:
            report["experiments"]["probes_sensitivity"] = experiment_probes(client, args)

        # --- Experiment 4: Dimension ---
        if not args.skip_dimension:
            report["experiments"]["dimension"] = experiment_dimension(client, args)

        # --- Experiment 5: General Ops ---
        if not args.skip_general:
            report["experiments"]["general_ops"] = experiment_general_ops(client, args)

        client.close()

    finally:
        stop_observer(observer_proc)

    # Save results
    output_path = os.path.join(SCRIPT_DIR, args.output)
    with open(output_path, "w") as f:
        json.dump(report, f, indent=2, ensure_ascii=False)
    print(f"\nResults saved to {output_path}")

    # Print summary
    _print_summary(report)


def _print_summary(report: Dict):
    """Print a human-readable summary of results."""
    print("\n" + "=" * 70)
    print("BENCHMARK SUMMARY")
    print("=" * 70)

    for exp_name, exp_data in report.get("experiments", {}).items():
        exp_meta = exp_data.get("experiment", exp_name)
        results = exp_data.get("results", [])

        if not results:DATA_DIR
            continue

        print(f"\n--- {exp_meta} ---")

        if exp_meta == "data_scale":
            print(f"{'N':>8} {'Exact(ms)':>10} {'ANN(ms)':>10} {'Speedup':>8} {'Recall':>8}")
            print("-" * 50)
            for r in results:
                print(f"{r['data_size']:>8} {r['exact_latency_ms_avg']:>10.2f} "
                      f"{r['ann_latency_ms_avg']:>10.2f} {r['speedup_ratio']:>7.1f}x "
                      f"{r['recall_avg']:>7.4f}")

        elif exp_meta == "lists_sensitivity":
            print(f"{'lists':>8} {'ANN(ms)':>10} {'Recall':>8} {'Build(ms)':>10}")
            print("-" * 42)
            for r in results:
                print(f"{r['lists']:>8} {r['ann_latency_ms_avg']:>10.2f} "
                      f"{r['recall_avg']:>7.4f} {r['index_build_time_ms']:>10.0f}")

        elif exp_meta == "probes_sensitivity":
            print(f"{'probes':>8} {'ANN(ms)':>10} {'Recall':>8}")
            print("-" * 32)
            for r in results:
                print(f"{r['probes']:>8} {r['ann_latency_ms_avg']:>10.2f} "
                      f"{r['recall_avg']:>7.4f}")

        elif exp_meta == "dimension":
            print(f"{'dim':>8} {'Exact(ms)':>10} {'ANN(ms)':>10} {'Speedup':>8} {'Recall':>8}")
            print("-" * 50)
            for r in results:
                print(f"{r['vector_dim']:>8} {r['exact_latency_ms_avg']:>10.2f} "
                      f"{r['ann_latency_ms_avg']:>10.2f} {r['speedup_ratio']:>7.1f}x "
                      f"{r['recall_avg']:>7.4f}")

        elif exp_meta == "general_ops_comparison":
            r = results
            print(f"  {'Operation':<40} {'Latency(ms)':>12}")
            print("  " + "-" * 52)
            items = [
                ("Integer point query (btree)", r.get("integer_point_query_ms", 0)),
                ("Integer range scan (500 rows)", r.get("integer_range_scan_500rows_ms", 0)),
                ("Integer sort + limit top-k", r.get("integer_sort_limit_ms", 0)),
                ("Integer full table scan count(*)", r.get("integer_full_scan_count_ms", 0)),
                ("Vector exact search top-k", r.get("vector_exact_topk_ms", 0)),
                ("Vector ANN search top-k", r.get("vector_ann_topk_ms", 0)),
            ]
            for name, val in items:
                print(f"  {name:<40} {val:>10.3f}")
            print(f"\n  Vector ANN recall@{report['metadata']['config']['k']}: {r.get('vector_ann_recall', 0):.4f}")


if __name__ == "__main__":
    main()
