/* Copyright (c) 2021 OceanBase and/or its affiliates. All rights reserved.
miniob is licensed under Mulan PSL v2.
You can use this software according to the terms and conditions of the Mulan PSL v2.
You may obtain a copy of Mulan PSL v2 at:
         http://license.coscl.org.cn/MulanPSL2
THIS SOFTWARE IS PROVIDED ON AN "AS IS" BASIS, WITHOUT WARRANTIES OF ANY KIND,
EITHER EXPRESS OR IMPLIED, INCLUDING BUT NOT LIMITED TO NON-INFRINGEMENT,
MERCHANTABILITY OR FIT FOR A PARTICULAR PURPOSE.
See the Mulan PSL v2 for more details. */

#include "ivfflat_index.h"

#include <algorithm>
#include <cmath>
#include <fstream>
#include <cstring>

#include "common/log/log.h"
#include "storage/record/record_manager.h"

IvfflatIndex::~IvfflatIndex() noexcept = default;

RC IvfflatIndex::create(Table *table, const char *file_name, const IndexMeta &index_meta, const FieldMeta &field_meta)
{
  table_ = table;
  dim_ = field_meta.len() / sizeof(float);
  file_name_ = file_name;
  inited_ = true;
  return Index::init(index_meta, field_meta);
}

RC IvfflatIndex::open(Table *table, const char *file_name, const IndexMeta &index_meta, const FieldMeta &field_meta)
{
  table_ = table;
  dim_ = field_meta.len() / sizeof(float);
  file_name_ = file_name;
  inited_ = true;

  RC rc = Index::init(index_meta, field_meta);
  if (OB_FAIL(rc)) {
    return rc;
  }

  // read persisted data from file
  std::ifstream ifs(file_name_, std::ios::binary);
  if (!ifs.is_open()) {
    LOG_WARN("failed to open index file for reading: %s", file_name_.c_str());
    return RC::SUCCESS;  // empty index, not an error at open time
  }

  uint32_t magic, version;
  ifs.read(reinterpret_cast<char*>(&magic), sizeof(magic));
  ifs.read(reinterpret_cast<char*>(&version), sizeof(version));
  if (magic != 0x49564646 || version != 1) {
    LOG_WARN("invalid index file magic/version: %s", file_name_.c_str());
    return RC::IOERR_READ;
  }

  int32_t persisted_dim;
  ifs.read(reinterpret_cast<char*>(&persisted_dim), sizeof(persisted_dim));
  ifs.read(reinterpret_cast<char*>(&lists_), sizeof(lists_));
  ifs.read(reinterpret_cast<char*>(&probes_), sizeof(probes_));

  int32_t num_centers;
  ifs.read(reinterpret_cast<char*>(&num_centers), sizeof(num_centers));
  centers.resize(num_centers);
  for (int i = 0; i < num_centers; i++) {
    centers[i].resize(dim_);
    ifs.read(reinterpret_cast<char*>(centers[i].data()), dim_ * sizeof(float));
  }

  int32_t num_inv_lists;
  ifs.read(reinterpret_cast<char*>(&num_inv_lists), sizeof(num_inv_lists));
  invList.resize(num_inv_lists);
  for (int i = 0; i < num_inv_lists; i++) {
    int32_t num_entries;
    ifs.read(reinterpret_cast<char*>(&num_entries), sizeof(num_entries));
    invList[i].resize(num_entries);
    for (int j = 0; j < num_entries; j++) {
      invList[i][j].position.resize(dim_);
      ifs.read(reinterpret_cast<char*>(invList[i][j].position.data()), dim_ * sizeof(float));
      ifs.read(reinterpret_cast<char*>(&invList[i][j].rid.page_num), sizeof(invList[i][j].rid.page_num));
      ifs.read(reinterpret_cast<char*>(&invList[i][j].rid.slot_num), sizeof(invList[i][j].rid.slot_num));
    }
  }

  return RC::SUCCESS;
}

RC IvfflatIndex::close() { return RC::SUCCESS; }

RC IvfflatIndex::insert_entry(const char *record, const RID *rid)
{
  if (!inited_ || centers.empty()) {
    return RC::SUCCESS;  // not trained yet, skip
  }

  const float *vec_data = reinterpret_cast<const float*>(record + field_meta_.offset());
  std::vector<float> vec(vec_data, vec_data + dim_);

  int cid = assign(vec);
  if (cid < 0) {
    return RC::INTERNAL;
  }

  IvfEntry entry;
  entry.position = std::move(vec);
  entry.rid = *rid;
  invList[cid].push_back(std::move(entry));
  return RC::SUCCESS;
}

RC IvfflatIndex::delete_entry(const char *record, const RID *rid) { return RC::SUCCESS; }

IndexScanner *IvfflatIndex::create_scanner(const char *left_key, int left_len, bool left_inclusive,
    const char *right_key, int right_len, bool right_inclusive)
{
  return nullptr;
}

RC IvfflatIndex::sync()
{
  if (file_name_.empty()) {
    return RC::SUCCESS;
  }

  std::ofstream ofs(file_name_, std::ios::binary | std::ios::trunc);
  if (!ofs.is_open()) {
    LOG_WARN("failed to open index file for writing: %s", file_name_.c_str());
    return RC::IOERR_OPEN;
  }

  uint32_t magic = 0x49564646;   // "IVFF"
  uint32_t version = 1;
  int32_t dim = static_cast<int32_t>(dim_);
  int32_t lists = static_cast<int32_t>(lists_);
  int32_t probes = static_cast<int32_t>(probes_);

  ofs.write(reinterpret_cast<const char*>(&magic), sizeof(magic));
  ofs.write(reinterpret_cast<const char*>(&version), sizeof(version));
  ofs.write(reinterpret_cast<const char*>(&dim), sizeof(dim));
  ofs.write(reinterpret_cast<const char*>(&lists), sizeof(lists));
  ofs.write(reinterpret_cast<const char*>(&probes), sizeof(probes));

  // write centers
  int32_t num_centers = static_cast<int32_t>(centers.size());
  ofs.write(reinterpret_cast<const char*>(&num_centers), sizeof(num_centers));
  for (const auto &centroid : centers) {
    ofs.write(reinterpret_cast<const char*>(centroid.data()), dim_ * sizeof(float));
  }

  // write inverted lists
  int32_t num_inv_lists = static_cast<int32_t>(invList.size());
  ofs.write(reinterpret_cast<const char*>(&num_inv_lists), sizeof(num_inv_lists));
  for (const auto &list : invList) {
    int32_t num_entries = static_cast<int32_t>(list.size());
    ofs.write(reinterpret_cast<const char*>(&num_entries), sizeof(num_entries));
    for (const auto &entry : list) {
      ofs.write(reinterpret_cast<const char*>(entry.position.data()), dim_ * sizeof(float));
      ofs.write(reinterpret_cast<const char*>(&entry.rid.page_num), sizeof(entry.rid.page_num));
      ofs.write(reinterpret_cast<const char*>(&entry.rid.slot_num), sizeof(entry.rid.slot_num));
    }
  }

  ofs.close();
  return RC::SUCCESS;
}

RC IvfflatIndex::build(const std::vector<std::vector<float>> &vectors, const std::vector<RID> &rids)
{
  if (vectors.empty()) {
    return RC::SUCCESS;
  }
  train(vectors, rids);
  return sync();
}

void IvfflatIndex::train(const std::vector<std::vector<float>> &all_vectors, std::vector<RID> rids)
{
  int num_vectors = all_vectors.size();
  if (num_vectors == 0) return;

  int num_lists = std::min(lists_, num_vectors);

  // 1. random init centroids
  centers.resize(num_lists);
  for (int i = 0; i < num_lists; i++) {
    centers[i] = all_vectors[i % num_vectors];
  }

  std::vector<int> assignments(num_vectors, 0);
  const int max_iter = 100;
  bool changed = true;

  // 2. k-means iteration
  for (int iter = 0; iter < max_iter && changed; iter++) {
    changed = false;

    // a. assign
    for (int i = 0; i < num_vectors; i++) {
      int best = 0;
      float best_dist = L2_distance(all_vectors[i], centers[0], dim_);
      for (int c = 1; c < num_lists; c++) {
        float d = L2_distance(all_vectors[i], centers[c], dim_);
        if (d < best_dist) {
          best_dist = d;
          best = c;
        }
      }
      if (assignments[i] != best) {
        assignments[i] = best;
        changed = true;
      }
    }

    // b. update centroids
    std::vector<std::vector<float>> new_centroids(num_lists, std::vector<float>(dim_, 0));
    std::vector<int> counts(num_lists, 0);
    for (int i = 0; i < num_vectors; i++) {
      for (int d = 0; d < dim_; d++) {
        new_centroids[assignments[i]][d] += all_vectors[i][d];
      }
      counts[assignments[i]]++;
    }
    for (int c = 0; c < num_lists; c++) {
      if (counts[c] > 0) {
        for (int d = 0; d < dim_; d++) {
          new_centroids[c][d] /= counts[c];
        }
      } else {
        new_centroids[c] = centers[c];  // keep old centroid
      }
    }
    centers = std::move(new_centroids);
  }

  // 3. build inverted lists
  invList.resize(num_lists);
  for (int i = 0; i < num_vectors; i++) {
    IvfEntry entry;
    entry.position = all_vectors[i];
    entry.rid = rids[i];
    invList[assignments[i]].push_back(std::move(entry));
  }
}

float IvfflatIndex::L2_distance(const std::vector<float> &x1, const std::vector<float> &y1, int dim)
{
  float base = 0;
  if (x1.size() != y1.size() || static_cast<int>(x1.size()) != dim) {
    return -1;
  }
  for (int d = 0; d < dim; d++) {
    float diff = x1[d] - y1[d];
    base += diff * diff;
  }
  return std::sqrt(base);
}

int IvfflatIndex::assign(const std::vector<float> &base_vector)
{
  if (centers.empty()) return -1;
  int best = 0;
  float best_dist = L2_distance(base_vector, centers[0], dim_);
  for (size_t c = 1; c < centers.size(); c++) {
    float d = L2_distance(base_vector, centers[c], dim_);
    if (d < best_dist) {
      best_dist = d;
      best = static_cast<int>(c);
    }
  }
  return best;
}

vector<RID> IvfflatIndex::ann_search(const vector<float> &base_vector, size_t limit)
{
  std::vector<std::pair<float, int>> distances;
  for (size_t i = 0; i < centers.size(); i++) {
    distances.emplace_back(L2_distance(base_vector, centers[i], dim_), static_cast<int>(i));
  }
  std::sort(distances.begin(), distances.end(),
      [](const std::pair<float, int> &x, const std::pair<float, int> &y) { return x.first < y.first; });

  std::vector<std::pair<float, RID>> distance_res;
  int num_probe = std::min(probes_, static_cast<int>(centers.size()));
  for (int i = 0; i < num_probe; i++) {
    int cid = distances[i].second;
    for (const auto &x : invList[cid]) {
      float dist = L2_distance(base_vector, x.position, dim_);
      distance_res.emplace_back(dist, x.rid);
    }
  }

  std::sort(distance_res.begin(), distance_res.end(),
      [](const std::pair<float, RID> &x, const std::pair<float, RID> &y) { return x.first < y.first; });

  std::vector<RID> rids;
  size_t result_count = std::min(limit, distance_res.size());
  rids.reserve(result_count);
  for (size_t i = 0; i < result_count; i++) {
    rids.push_back(distance_res[i].second);
  }
  return rids;
}
