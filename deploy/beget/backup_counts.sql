SELECT COALESCE(json_object_agg(format('%I.%I',schemaname,tablename),
 (xpath('/row/c/text()',query_to_xml(format('SELECT count(*) AS c FROM %I.%I',schemaname,tablename),false,true,'')))[1]::text::bigint),'{}'::json)
FROM pg_tables WHERE schemaname='public';
