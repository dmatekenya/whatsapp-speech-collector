<?php
declare(strict_types=1);
// Run ONCE from the protected Hostinger terminal after backing up DB and website.
if(PHP_SAPI!=='cli'){http_response_code(404);exit;}
require dirname(__DIR__).'/app/bootstrap.php';
$pdo=db();
$schema=require dirname(__DIR__).'/app/schema.php';
foreach($schema as $sql)$pdo->exec($sql);
$cols=$pdo->query("SELECT COLUMN_NAME FROM information_schema.COLUMNS WHERE TABLE_SCHEMA=DATABASE() AND TABLE_NAME='review_drafts'")->fetchAll(PDO::FETCH_COLUMN);
$updates=[
 'reviewer_name_snapshot'=>"ALTER TABLE review_drafts ADD COLUMN reviewer_name_snapshot VARCHAR(120) NULL AFTER reviewer_id",
 'production_state'=>"ALTER TABLE review_drafts ADD COLUMN production_state ENUM('draft','pending','confirmed','failed','conflict') NOT NULL DEFAULT 'draft' AFTER listened",
 'operation_key'=>"ALTER TABLE review_drafts ADD COLUMN operation_key CHAR(36) NULL AFTER production_state",
 'expected_qc'=>"ALTER TABLE review_drafts ADD COLUMN expected_qc VARCHAR(30) NULL AFTER operation_key",
 'expected_tier3'=>"ALTER TABLE review_drafts ADD COLUMN expected_tier3 VARCHAR(10) NULL AFTER expected_qc",
 'reason_code'=>"ALTER TABLE review_drafts ADD COLUMN reason_code VARCHAR(32) NULL AFTER expected_tier3",
 'production_qc'=>"ALTER TABLE review_drafts ADD COLUMN production_qc VARCHAR(30) NULL AFTER expected_qc",
 'production_message'=>"ALTER TABLE review_drafts ADD COLUMN production_message VARCHAR(255) NULL AFTER production_qc",
 'confirmed_at'=>"ALTER TABLE review_drafts ADD COLUMN confirmed_at DATETIME NULL AFTER production_message",
];
foreach($updates as $name=>$sql){if(!in_array($name,$cols,true)){$pdo->exec($sql);echo "Added $name\n";}else echo "Existing $name\n";}
$recordingCols=$pdo->query("SELECT COLUMN_NAME FROM information_schema.COLUMNS WHERE TABLE_SCHEMA=DATABASE() AND TABLE_NAME='recordings'")->fetchAll(PDO::FETCH_COLUMN);
if(!in_array('prompt_type',$recordingCols,true)){
 $pdo->exec('ALTER TABLE recordings ADD COLUMN prompt_type VARCHAR(48) NULL AFTER qc_reason');
 echo "Added recordings.prompt_type cache column\n";
}else echo "Existing recordings.prompt_type cache column\n";
foreach([
 'human_decision'=>'ALTER TABLE recordings ADD COLUMN human_decision VARCHAR(10) NULL AFTER prompt_type',
 'human_operation_key'=>'ALTER TABLE recordings ADD COLUMN human_operation_key CHAR(36) NULL AFTER human_decision',
 'human_reviewed_at'=>'ALTER TABLE recordings ADD COLUMN human_reviewed_at DATETIME NULL AFTER human_operation_key'
] as $name=>$sql){
 if(!in_array($name,$recordingCols,true)){$pdo->exec($sql);echo "Added recordings.$name\n";}
 else echo "Existing recordings.$name\n";
}
// Preserve historical draft values while allowing canonical accept/reject submissions.
$decisionColumn=$pdo->query("SELECT COLUMN_TYPE FROM information_schema.COLUMNS WHERE TABLE_SCHEMA=DATABASE() AND TABLE_NAME='review_drafts' AND COLUMN_NAME='decision'")->fetchColumn();
if(is_string($decisionColumn) && !str_contains($decisionColumn,"'accept'")){
 $pdo->exec("ALTER TABLE review_drafts MODIFY COLUMN decision ENUM('flag','approve','accept','reject','escalate') NOT NULL DEFAULT 'flag'");
 echo "Enabled canonical accept decision (historical drafts retained)\n";
}
foreach([
 'qc_tier2'=>'TINYINT UNSIGNED NULL',
 'qc_tier3'=>'VARCHAR(10) NULL',
 'qc_tier3_reason'=>'VARCHAR(80) NULL',
 'qc_final'=>'VARCHAR(12) NULL',
 'band_reason'=>'VARCHAR(200) NULL',
 'source_updated_at'=>'DATETIME(6) NULL'
] as $name=>$type){
 if(!in_array($name,$recordingCols,true)){
  $pdo->exec("ALTER TABLE recordings ADD COLUMN `$name` $type");
  echo "Added owner QC cache column $name\n";
 }else echo "Existing owner QC cache column $name\n";
}
$idx=(int)query("SELECT COUNT(*) FROM information_schema.STATISTICS WHERE TABLE_SCHEMA=DATABASE() AND TABLE_NAME='review_drafts' AND INDEX_NAME='uq_review_operation'")->fetchColumn();
if(!$idx){$pdo->exec('ALTER TABLE review_drafts ADD UNIQUE INDEX uq_review_operation (operation_key)');echo "Added unique operation index\n";}
// Existing reviews were drafts and are correctly preserved as such.
if(setting('refresh_seconds')===null)set_setting('refresh_seconds','5');
echo "Online dashboard schema upgrade completed. Existing accounts, assignments, reviews and production snapshot records were preserved.\n";
