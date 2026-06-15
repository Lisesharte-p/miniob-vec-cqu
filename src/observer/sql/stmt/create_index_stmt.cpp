/* Copyright (c) 2021 OceanBase and/or its affiliates. All rights reserved.
miniob is licensed under Mulan PSL v2.
You can use this software according to the terms and conditions of the Mulan PSL v2.
You may obtain a copy of Mulan PSL v2 at:
         http://license.coscl.org.cn/MulanPSL2
THIS SOFTWARE IS PROVIDED ON AN "AS IS" BASIS, WITHOUT WARRANTIES OF ANY KIND,
EITHER EXPRESS OR IMPLIED, INCLUDING BUT NOT LIMITED TO NON-INFRINGEMENT,
MERCHANTABILITY OR FIT FOR A PARTICULAR PURPOSE.
See the Mulan PSL v2 for more details. */

//
// Created by Wangyunlai on 2023/4/25.
//

#include "sql/stmt/create_index_stmt.h"
#include "common/lang/string.h"
#include "common/log/log.h"
#include "storage/db/db.h"
#include "storage/table/table.h"

using namespace std;
using namespace common;

RC CreateIndexStmt::create(Db *db, const CreateIndexSqlNode &create_index, Stmt *&stmt)
{
  stmt = nullptr;

  const char *table_name = create_index.relation_name.c_str();
  if (is_blank(table_name) || is_blank(create_index.index_name.c_str()) ||
      is_blank(create_index.attribute_name.c_str())) {
    LOG_WARN("invalid argument. db=%p, table_name=%p, index name=%s, attribute name=%s",
        db, table_name, create_index.index_name.c_str(), create_index.attribute_name.c_str());
    return RC::INVALID_ARGUMENT;
  }

  // check whether the table exists
  Table *table = db->find_table(table_name);
  if (nullptr == table) {
    LOG_WARN("no such table. db=%s, table_name=%s", db->name(), table_name);
    return RC::SCHEMA_TABLE_NOT_EXIST;
  }

  const FieldMeta *field_meta = table->table_meta().field(create_index.attribute_name.c_str());
  if (nullptr == field_meta) {
    LOG_WARN("no such field in table. db=%s, table=%s, field name=%s",
             db->name(), table_name, create_index.attribute_name.c_str());
    return RC::SCHEMA_FIELD_NOT_EXIST;
  }

  if (create_index.is_vector_index && field_meta->type() != AttrType::VECTORS) {
    LOG_WARN("vector index can only be created on VECTORS type field. field=%s", create_index.attribute_name.c_str());
    return RC::INVALID_ARGUMENT;
  }

  Index *index = table->find_index(create_index.index_name.c_str());
  if (nullptr != index) {
    LOG_WARN("index with name(%s) already exists. table name=%s", create_index.index_name.c_str(), table_name);
    return RC::SCHEMA_INDEX_NAME_REPEAT;
  }

  if (create_index.is_vector_index) {
    if (create_index.lists > 0 && create_index.lists > 65536) {
      LOG_WARN("lists parameter out of range: %d", create_index.lists);
      return RC::INVALID_ARGUMENT;
    }
    if (create_index.probes > 0 && create_index.probes > create_index.lists) {
      LOG_WARN("probes (%d) cannot exceed lists (%d)", create_index.probes, create_index.lists);
      return RC::INVALID_ARGUMENT;
    }
  }

  stmt = new CreateIndexStmt(table, field_meta, create_index.index_name, create_index.is_vector_index,
                              create_index.lists, create_index.probes);
  return RC::SUCCESS;
}
