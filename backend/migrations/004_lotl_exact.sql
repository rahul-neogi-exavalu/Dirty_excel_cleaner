-- The LOTL holds the business's pc_name_pc_number_from_lotl workbook exactly as written:
-- all its rows, the text 'null' kept, nothing trimmed. A database where 003 loaded it
-- differently is emptied here; the service fills it again from assets/ right after the
-- migrations run (it fills the table only while it is empty).
DELETE FROM {control}.lotl;
