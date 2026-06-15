/* Copyright (c) 2021 OceanBase and/or its affiliates. All rights reserved.
miniob is licensed under Mulan PSL v2.
You can use this software according to the terms and conditions of the Mulan PSL v2.
You may obtain a copy of Mulan PSL v2 at:
         http://license.coscl.org.cn/MulanPSL2
THIS SOFTWARE IS PROVIDED ON AN "AS IS" BASIS, WITHOUT WARRANTIES OF ANY KIND,
EITHER EXPRESS OR IMPLIED, INCLUDING BUT NOT LIMITED TO NON-INFRINGEMENT,
MERCHANTABILITY OR FIT FOR A PARTICULAR PURPOSE.
See the Mulan PSL v2 for more details. */

#pragma once

#include <string>
#include <vector>
#include "storage/index/index.h"

/**
 * @brief ivfflat 向量索引
 * @ingroup Index
 */
struct IvfEntry
{
  std::vector<float> position;
  RID rid;
};

class IvfflatIndex : public Index
{
public:
  IvfflatIndex(int list, int probes) : lists_{list}, probes_{probes} {};
  virtual ~IvfflatIndex() noexcept;

  RC create(Table *table, const char *file_name, const IndexMeta &index_meta, const FieldMeta &field_meta) override;

  RC open(Table *table, const char *file_name, const IndexMeta &index_meta, const FieldMeta &field_meta) override;

  bool is_vector_index() override { return true; }

  vector<RID> ann_search(const vector<float> &base_vector, size_t limit);

  RC close();

  RC insert_entry(const char *record, const RID *rid) override;
  RC delete_entry(const char *record, const RID *rid) override;

  IndexScanner *create_scanner(const char *left_key, int left_len, bool left_inclusive,
      const char *right_key, int right_len, bool right_inclusive) override;

  RC sync() override;

  // vector index specific
  RC build(const std::vector<std::vector<float>> &vectors, const std::vector<RID> &rids);

private:
  void train(const std::vector<std::vector<float>> &base_vector, std::vector<RID> rids);
  static float L2_distance(const std::vector<float> &x1, const std::vector<float> &y1, int dim);
  int assign(const std::vector<float> &base_vector);

  int    dim_      = 0;
  bool   inited_   = false;
  Table *table_    = nullptr;
  int    lists_    = 245;  ///< number of cluster centroids for IVF
  int    probes_   = 5;    ///< number of probes at search time
  std::string file_name_;
  std::vector<std::vector<float>> centers;
  std::vector<std::vector<IvfEntry>> invList;
};
