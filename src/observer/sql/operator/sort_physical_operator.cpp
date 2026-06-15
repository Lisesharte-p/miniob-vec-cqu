/* Copyright (c) 2021 OceanBase and/or its affiliates. All rights reserved.
miniob is licensed under Mulan PSL v2.
You can use this software according to the terms and conditions of the Mulan PSL v2.
You may obtain a copy of Mulan PSL v2 at:
         http://license.coscl.org.cn/MulanPSL2
THIS SOFTWARE IS PROVIDED ON AN "AS IS" BASIS, WITHOUT WARRANTIES OF ANY KIND,
EITHER EXPRESS OR IMPLIED, INCLUDING BUT NOT LIMITED TO NON-INFRINGEMENT,
MERCHANTABILITY OR FIT FOR A PARTICULAR PURPOSE.
See the Mulan PSL v2 for more details. */

#include "sql/operator/sort_physical_operator.h"
#include "common/log/log.h"
#include "sql/expr/expression.h"
#include "sql/expr/tuple.h"

#include <algorithm>

using namespace std;

SortPhysicalOperator::SortPhysicalOperator(vector<unique_ptr<Expression>> &&order_by_expressions, int limit)
    : order_by_expressions_(std::move(order_by_expressions)), limit_(limit)
{}

RC SortPhysicalOperator::open(Trx *trx)
{
  if (children_.empty()) {
    LOG_WARN("sort operator must have a child");
    return RC::INTERNAL;
  }

  RC rc = children_[0]->open(trx);
  if (rc != RC::SUCCESS) {
    LOG_WARN("failed to open child operator: %s", strrc(rc));
    return rc;
  }

  const bool topk_mode = (limit_ > 0);

  // materialize all tuples
  while (RC::SUCCESS == (rc = children_[0]->next())) {
    Tuple *tuple = children_[0]->current_tuple();
    if (nullptr == tuple) {
      return RC::INTERNAL;
    }

    SortTuple st;
    // materialize the full tuple
    rc = ValueListTuple::make(*tuple, st.tuple);
    if (OB_FAIL(rc)) {
      LOG_WARN("failed to materialize tuple: %s", strrc(rc));
      return rc;
    }

    // pre-compute order-by key values
    for (const auto &expr : order_by_expressions_) {
      Value value;
      rc = expr->get_value(*tuple, value);
      if (OB_FAIL(rc)) {
        LOG_WARN("failed to evaluate order-by expression: %s", strrc(rc));
        return rc;
      }
      st.order_values.push_back(value);
    }

    if (topk_mode) {
      // Top-K: maintain a max-heap of size K
      // heap top = largest = worst in ASC order
      if ((int)sorted_tuples_.size() < limit_) {
        sorted_tuples_.push_back(std::move(st));
        push_heap(sorted_tuples_.begin(), sorted_tuples_.end(), less_by_order);
      } else if (less_by_order(st, sorted_tuples_[0])) {
        // new row is better (smaller) than the current worst
        pop_heap(sorted_tuples_.begin(), sorted_tuples_.end(), less_by_order);
        sorted_tuples_.back() = std::move(st);
        push_heap(sorted_tuples_.begin(), sorted_tuples_.end(), less_by_order);
      }
    } else {
      sorted_tuples_.push_back(std::move(st));
    }
  }

  if (rc != RC::RECORD_EOF) {
    LOG_WARN("unexpected error while reading child tuples: %s", strrc(rc));
    return rc;
  }

  // final sort
  if (topk_mode) {
    // heap -> sorted ASC
    sort(sorted_tuples_.begin(), sorted_tuples_.end(), less_by_order);
  } else {
    sort_tuples();
  }

  current_index_ = 0;
  return RC::SUCCESS;
}

RC SortPhysicalOperator::next()
{
  if (current_index_ >= sorted_tuples_.size()) {
    return RC::RECORD_EOF;
  }
  current_index_++;
  return RC::SUCCESS;
}

RC SortPhysicalOperator::close()
{
  sorted_tuples_.clear();
  current_index_ = 0;
  if (!children_.empty()) {
    return children_[0]->close();
  }
  return RC::SUCCESS;
}

Tuple *SortPhysicalOperator::current_tuple()
{
  if (current_index_ == 0 || current_index_ > sorted_tuples_.size()) {
    return nullptr;
  }
  return &sorted_tuples_[current_index_ - 1].tuple;
}

RC SortPhysicalOperator::tuple_schema(TupleSchema &schema) const
{
  return children_[0]->tuple_schema(schema);
}

void SortPhysicalOperator::sort_tuples()
{
  sort(sorted_tuples_.begin(), sorted_tuples_.end(), less_by_order);
}

bool SortPhysicalOperator::less_by_order(const SortTuple &a, const SortTuple &b)
{
  for (size_t i = 0; i < a.order_values.size() && i < b.order_values.size(); i++) {
    int cmp = a.order_values[i].compare(b.order_values[i]);
    if (cmp != 0) {
      return cmp < 0;
    }
  }
  return false;
}
