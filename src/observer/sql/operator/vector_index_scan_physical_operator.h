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

#include <vector>

#include "sql/operator/physical_operator.h"
#include "storage/record/record_manager.h"

class Table;
class IvfflatIndex;

/**
 * @brief 向量索引扫描物理算子
 * @ingroup PhysicalOperator
 *
 * 使用 IvfflatIndex 的 ANN 搜索来高效地返回最近的 K 个向量。
 * 在 open() 中执行搜索，在 next() 中按 RID 逐条获取记录。
 */
class VectorIndexScanPhysicalOperator : public PhysicalOperator
{
public:
  VectorIndexScanPhysicalOperator(Table *table, IvfflatIndex *index,
      const std::vector<float> &query_vector, size_t limit);

  virtual ~VectorIndexScanPhysicalOperator() = default;

  PhysicalOperatorType type() const override { return PhysicalOperatorType::VECTOR_INDEX_SCAN; }

  RC open(Trx *trx) override;
  RC next() override;
  RC close() override;
  Tuple *current_tuple() override;

private:
  Table        *table_         = nullptr;
  IvfflatIndex *index_         = nullptr;
  std::vector<float> query_vector_;
  size_t        limit_         = 10;

  std::vector<RID> results_;
  size_t           current_pos_ = 0;
  Record           current_record_;
  RowTuple         tuple_;
  Trx             *trx_ = nullptr;
};
