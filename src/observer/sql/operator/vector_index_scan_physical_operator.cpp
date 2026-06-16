/* Copyright (c) 2021 OceanBase and/or its affiliates. All rights reserved.
miniob is licensed under Mulan PSL v2.
You can use this software according to the terms and conditions of the Mulan PSL v2.
You may obtain a copy of Mulan PSL v2 at:
         http://license.coscl.org.cn/MulanPSL2
THIS SOFTWARE IS PROVIDED ON AN "AS IS" BASIS, WITHOUT WARRANTIES OF ANY KIND,
EITHER EXPRESS OR IMPLIED, INCLUDING BUT NOT LIMITED TO NON-INFRINGEMENT,
MERCHANTABILITY OR FIT FOR A PARTICULAR PURPOSE.
See the Mulan PSL v2 for more details. */

#include "vector_index_scan_physical_operator.h"

#include "common/log/log.h"
#include "storage/index/ivfflat_index.h"
#include "storage/table/table.h"
#include "storage/trx/trx.h"

VectorIndexScanPhysicalOperator::VectorIndexScanPhysicalOperator(
    Table *table, IvfflatIndex *index,
    const std::vector<float> &query_vector, size_t limit)
    : table_(table), index_(index), query_vector_(query_vector), limit_(limit)
{}

RC VectorIndexScanPhysicalOperator::open(Trx *trx)
{
  if (table_ == nullptr || index_ == nullptr) {
    return RC::INTERNAL;
  }

  trx_ = trx;

  // set up tuple schema
  tuple_.set_schema(table_, table_->table_meta().field_metas());

  // run ANN search
  results_ = index_->ann_search(query_vector_, limit_);
  current_pos_ = 0;

  LOG_TRACE("vector index scan opened, found %zu results, limit=%zu", results_.size(), limit_);
  return RC::SUCCESS;
}

RC VectorIndexScanPhysicalOperator::next()
{
  RC rc = RC::SUCCESS;
  while (current_pos_ < results_.size()) {
    const RID &rid = results_[current_pos_];
    current_pos_++;

    rc = table_->get_record(rid, current_record_);
    if (OB_FAIL(rc)) {
      LOG_WARN("failed to get record. rid=%s, rc=%s", rid.to_string().c_str(), strrc(rc));
      return rc;
    }

    tuple_.set_record(&current_record_);

    // check transaction visibility
    rc = trx_->visit_record(table_, current_record_, ReadWriteMode::READ_ONLY);
    if (rc == RC::RECORD_INVISIBLE) {
      continue;
    }
    return rc;
  }
  return RC::RECORD_EOF;
}

Tuple *VectorIndexScanPhysicalOperator::current_tuple()
{
  tuple_.set_record(&current_record_);
  return &tuple_;
}

RC VectorIndexScanPhysicalOperator::close()
{
  results_.clear();
  current_pos_ = 0;
  trx_ = nullptr;
  return RC::SUCCESS;
}
