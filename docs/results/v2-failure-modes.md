# Stage 2 skeleton — v2-full-5e failure modes

Checkpoint: `target/checkpoints/skeleton-v2-full-5e/`

Eval: Spider 760 rows, BIRD 1068 rows. Greedy bf16 on CUDA, max_src=256, max_new_tokens=96.


## Spider — 760 rows, 326 failures (EM=57.11%)

| bucket | count | % of failures |
|---|---|---|
| slot count off | 147 | 45.1% |
| other | 90 | 27.6% |
| placeholder mismatch | 63 | 19.3% |
| structural | 26 | 8.0% |

### Spider structural diff (clauses)

| clause | model added (gold has not) | model dropped (gold has) |
|---|---|---|
| GROUP BY | 7 | 12 |
| ORDER BY | 2 | 5 |
| LIMIT | 0 | 5 |
| WHERE | 0 | 2 |
| HAVING | 2 | 0 |

### Spider top "other" failures (verbatim)

- **NL**: What are the names and release years for all the songs of the youngest singer?
  - gold: `SELECT @field1, @field2 FROM @entity1 ORDER BY @field3 LIMIT 1`
  - pred: `SELECT @field1, @field2 FROM @entity1 ORDER BY @field3 DESC LIMIT 1`
- **NL**: What is the average and maximum capacities for all stadiums ?
  - gold: `SELECT AVG(@field1), MAX(@field1) FROM @entity1`
  - pred: `SELECT AVG(@field1), AVG(@field1) FROM @entity1`
- **NL**: How many concerts occurred in 2014 or 2015?
  - gold: `SELECT COUNT(*) FROM @entity1 WHERE @field1 = @val1 OR @field1 = @val2`
  - pred: `SELECT COUNT(*) FROM @entity1 WHERE @field1 >= @val1 AND @field1 = @val2`
- **NL**: what is the name and nation of the singer who have a song having 'Hey' in its name?
  - gold: `SELECT @field1, @field2 FROM @entity1 WHERE @field3 LIKE @val1`
  - pred: `SELECT @field1, @field2 FROM @entity1 WHERE @field3 = @val1`
- **NL**: Find the weight of the youngest dog.
  - gold: `SELECT @field1 FROM @entity1 ORDER BY @field2 LIMIT 1`
  - pred: `SELECT @field1 FROM @entity1 ORDER BY @field2 DESC LIMIT 1`

### Spider input-length overflow check

- avg tokenized src len  fail=32.9  succ=28.4
- truncated (>256 tokens)  fail=0/326  succ=0/434


## BIRD — 1068 rows, 627 failures (EM=41.29%)

| bucket | count | % of failures |
|---|---|---|
| slot count off | 466 | 74.3% |
| other | 84 | 13.4% |
| placeholder mismatch | 40 | 6.4% |
| structural | 37 | 5.9% |

### BIRD structural diff (clauses)

| clause | model added (gold has not) | model dropped (gold has) |
|---|---|---|
| GROUP BY | 15 | 4 |
| LIMIT | 3 | 14 |
| ORDER BY | 3 | 11 |
| WHERE | 2 | 8 |
| HAVING | 2 | 1 |

### BIRD top "other" failures (verbatim)

- **NL**: Please list the phone numbers of the direct charter-funded schools that are opened after 2000/1/1.
  - gold: `SELECT @field1 FROM @entity1 WHERE @field2 = @val1 AND @field3 = @val2 AND @field4 > @val3`
  - pred: `SELECT @field1 FROM @entity1 WHERE @field2 = @val1 AND @field3 > @val2 AND @field4 = @val3`
- **NL**: Among the schools with the SAT test takers of over 500, please list the schools that are magnet schools or offer a magnet program.
  - gold: `SELECT @field1 FROM @entity1 WHERE @field2 = @val1 AND @field3 > @val2`
  - pred: `SELECT @field1 FROM @entity1 WHERE @field2 > @val1 AND @field3 > @val2`
- **NL**: How many schools in merged Alameda have number of test takers less than 100?
  - gold: `SELECT COUNT(@field1) FROM @entity1 WHERE @field2 = @val1 AND @field3 < @val2 AND @field4 = @val3`
  - pred: `SELECT COUNT(@field1) FROM @entity1 WHERE @field2 = @val1 AND @field3  @val2 AND @field4 = @val3`
- **NL**: What is the lowest grade for the District Special Education Consortia School with National Center for Educational Statistics school district identification number of 0613360?
  - gold: `SELECT MIN(@field1) FROM @entity1 WHERE @field2 = @val1 AND @field3 = @val2`
  - pred: `SELECT MAX(@field1) FROM @entity1 WHERE @field2 = @val1 AND @field3 = @val2`
- **NL**: How many accounts who choose issuance after transaction are staying in East Bohemia region?
  - gold: `SELECT COUNT(@field1) FROM @entity1 WHERE @field2 = @val1 AND @field3 = @val2`
  - pred: `SELECT COUNT(@field1) FROM @entity1 WHERE @field2 = @val1 AND @field3 > @val2`

### BIRD input-length overflow check

- avg tokenized src len  fail=40.6  succ=35.3
- truncated (>256 tokens)  fail=0/627  succ=0/441
