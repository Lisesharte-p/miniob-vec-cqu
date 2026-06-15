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

#include "common/value.h"
#include "common/type/data_type.h"

#include <cmath>

/**
 * @brief 向量类型
 * @ingroup DataType
 */
class VectorType : public DataType
{
public:
  VectorType() : DataType(AttrType::VECTORS) {}
  virtual ~VectorType() {}

  int compare(const Value &left, const Value &right) const override
  {
    if (left.length() != right.length()) {
      return 1;
    }
    for (int i = 0; i < left.length(); i++) {
      if (left.data()[i] != right.data()[i]) {
        return 1;
      }
    }
    return 0;
  }

  RC add(const Value &left, const Value &right, Value &result) const override { return RC::UNIMPLEMENTED; }
  RC subtract(const Value &left, const Value &right, Value &result) const override { return RC::UNIMPLEMENTED; }
  RC multiply(const Value &left, const Value &right, Value &result) const override { return RC::UNIMPLEMENTED; }
  float vector_distance(const Value &left, const Value &right,string distance_type) const override
  {
    int dim = left.length() / static_cast<int>(sizeof(float));
    const float *left_data = reinterpret_cast<const float*>(left.data());
    const float *right_data = reinterpret_cast<const float*>(right.data());

    if (distance_type=="EUCLIDEAN") {
      float sum = 0;
      for (int i = 0; i < dim; i++) {
        float diff = left_data[i] - right_data[i];
        sum += diff * diff;
      }
      return sqrt(sum);
    }
    if (distance_type=="DOT") {
      float dot = 0;
      for (int i = 0; i < dim; i++) {
        dot += left_data[i] * right_data[i];
      }
      return dot;
    }
    if (distance_type=="cosine") {
      float dot = 0, left_norm = 0, right_norm = 0;
      for (int i = 0; i < dim; i++) {
        dot += left_data[i] * right_data[i];
        left_norm += left_data[i] * left_data[i];
        right_norm += right_data[i] * right_data[i];
      }
      left_norm = sqrt(left_norm);
      right_norm = sqrt(right_norm);
      float norm_product = left_norm * right_norm;
      if (norm_product < 1e-9) {
        return 0;
      }
      return dot / norm_product;
    }
    return 0;
  }
  RC to_string(const Value &val, string &result) const override { return RC::UNIMPLEMENTED; }
};