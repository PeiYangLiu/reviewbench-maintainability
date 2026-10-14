#!/usr/bin/env bash
set -euo pipefail

rm -rf .oracle target
mkdir -p .oracle/test-reports

/oracle-tools/maven/bin/mvn \
  -B -ntp -Dstyle.color=never -Djacoco.skip=true \
  -Dmaven.test.skip=true package

jar_path="$(
  find target -maxdepth 1 -name 'mapping-template-*.jar' \
    ! -name 'original-*' ! -name '*-sources.jar' ! -name '*-javadoc.jar' \
    -print -quit
)"
test -n "$jar_path"

java -jar "$jar_path" \
  --basepath examples/rml-csv \
  --input-format csv \
  --input example.csv \
  --template template.vm \
  --format turtle \
  --output ../../.oracle/rml-csv-output.ttl

grep -F 'ex:6523' .oracle/rml-csv-output.ttl
grep -F 'transit:route "25"^^xsd:int' .oracle/rml-csv-output.ttl
grep -F 'wgs84_pos:lat "50.901389"' .oracle/rml-csv-output.ttl
grep -F 'wgs84_pos:long "4.484444"' .oracle/rml-csv-output.ttl

cat > .oracle/test-reports/mapping-template-cli.xml <<'XML'
<testsuite name="MappingTemplatePublicCli" tests="1" failures="0" errors="0">
  <testcase classname="MappingTemplatePublicCli" name="rmlCsvExampleProducesExpectedRdf"/>
</testsuite>
XML
