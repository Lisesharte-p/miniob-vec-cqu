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

#include "sql/operator/physical_operator.h"
#include "sql/expr/tuple.h"
#include "sql/expr/expression.h"

/**
 * @brief 排序
 * @ingroup PhysicalOperator
 */
class SortPhysicalOperator : public PhysicalOperator
{
public:
  SortPhysicalOperator(vector<unique_ptr<Expression>> &&order_by_expressions, int limit = -1);
  virtual ~SortPhysicalOperator() = default;

  PhysicalOperatorType type() const override { return PhysicalOperatorType::SORT; }
  OpType               get_op_type() const override { return OpType::ORDERBY; }

  RC open(Trx *trx) override;
  RC next() override;
  RC close() override;

  Tuple *current_tuple() override;
  RC     tuple_schema(TupleSchema &schema) const override;

private:
  struct SortTuple {
    ValueListTuple tuple;
    vector<Value>  order_values;
  };

  void sort_tuples();

  static bool less_by_order(const SortTuple &a, const SortTuple &b);

  vector<unique_ptr<Expression>> order_by_expressions_;
  int                            limit_ = -1;  ///< Top-K limit (-1 = sort all)

  vector<SortTuple> sorted_tuples_;
  size_t           current_index_ = 0;
};
