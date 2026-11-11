<?php
declare(strict_types=1);
function svg_icon(string $icon): string {
 $paths=[
 'grid'=>'<rect x="3" y="3" width="7" height="7" rx="2"/><rect x="14" y="3" width="7" height="7" rx="2"/><rect x="3" y="14" width="7" height="7" rx="2"/><rect x="14" y="14" width="7" height="7" rx="2"/>',
 'list'=>'<path d="M8 6h13M8 12h13M8 18h13M3 6h.01M3 12h.01M3 18h.01"/>',
 'users'=>'<circle cx="9" cy="8" r="3.5"/><path d="M2 20a7 7 0 0 1 14 0M17 5a3.5 3.5 0 0 1 0 7M17 16a5 5 0 0 1 5 4"/>',
 'chart'=>'<rect x="4" y="4" width="16" height="16" rx="2"/><path d="M8 16v-4m4 4V8m4 8v-6"/>',
 'shield'=>'<path d="M12 2 20 5v7c0 4-3 7-8 10-5-3-8-6-8-10V5z"/><path d="m9 12 2 2 4-4"/>',
 'bell'=>'<path d="M18 8a6 6 0 0 0-12 0c0 7-3 8-3 9h18c0-1-3-2-3-9ZM10 21h4"/>',
 'follow'=>'<path d="m3 18 7-7 4 4 7-8m-6 0h6v6"/>',
 'history'=>'<path d="M3 11a9 9 0 1 1 3 7M3 5v6h6M12 7v5l3 2"/>',
 'message'=>'<rect x="3" y="4" width="18" height="16" rx="3"/><path d="m4 7 8 6 8-6"/>',
 'menu'=>'<path d="M4 6h16M4 12h16M4 18h16"/>',
 'moon'=>'<path d="M20 16A9 9 0 0 1 8 4 9 9 0 1 0 20 16Z"/>',
 'sync'=>'<path d="M20 7v5h-5M4 17v-5h5"/><path d="M5 9a8 8 0 0 1 14-2l1 5M4 12l1 5a8 8 0 0 0 14-2"/>',
 'logout'=>'<path d="M10 4H5a2 2 0 0 0-2 2v12a2 2 0 0 0 2 2h5m6-4 5-4-5-4m5 4H9"/>',
 'search'=>'<circle cx="10" cy="10" r="7"/><path d="m16 16 5 5"/>',
 ];return '<svg class="ico" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-linecap="round" stroke-linejoin="round" stroke-width="1.8" aria-hidden="true">'.($paths[$icon]??$paths['grid']).'</svg>';
}
function nav_item(string $page,string $label,string $icon,string $current): string {return '<a href="'.h(linkto($page)).'" class="menu-link '.($current===$page?'active':'').'">'.svg_icon($icon).'<span>'.h($label).'</span>'.($page==='notifications'?'<b class="unread-pill" data-unread hidden>0</b>':'').'</a>';}
function page_top(string $title,string $subtitle='',string $kicker='PRODUCTION · SUPERVISION'): string {return '<div class="page-heading"><div><div class="eyebrow">'.h($kicker).'</div><h1>'.h($title).'</h1><p>'.h($subtitle).'</p></div></div>';}
function panel_start(string $title='',string $subtitle=''): string {return '<section class="panel"><div class="panel-head"><div><h2>'.h($title).'</h2>'.($subtitle!==''?'<p>'.h($subtitle).'</p>':'').'</div></div>';}
function box_metric(string $label,string $value,string $desc='',?string $page=null,array $params=[]): string {$tag=$page?'a':'div';$href=$page?' href="'.h(linkto($page,$params)).'"':'';return '<'.$tag.$href.' class="metric-card '.($page?'link-card':'').'"><div class="metric-label">'.h($label).'</div><div class="metric-value">'.h($value).'</div><div class="metric-desc">'.h($desc).'</div></'.$tag.'>';}
function pagination(int $page,int $total,int $perPage,array $params): string { $pages=max(1,(int)ceil($total/$perPage));if($pages<=1)return '<small class="muted">Page 1 of 1</small>'; $prev=max(1,$page-1);$next=min($pages,$page+1);return '<div class="pages"><span>Page '.h((string)$page).' of '.h((string)$pages).'</span><a class="btn outline'.($page<=1?' disabled':'').'" href="'.h(linkto($params['page']??'recordings',array_merge($params,['p'=>$prev]))).'">Previous</a><a class="btn outline'.($page>=$pages?' disabled':'').'" href="'.h(linkto($params['page']??'recordings',array_merge($params,['p'=>$next]))).'">Next</a></div>';}
function app_layout(string $page,string $title,string $content,array $user): void {
 $admin=$user['role']==='admin';$last=sync_timestamp();$ready=sync_ready();
 $seen=count_value('SELECT COUNT(*) FROM recordings r WHERE r.id > COALESCE((SELECT last_seen_recording_id FROM notification_state WHERE user_id=?),0)'.scope_sql($user)[0],array_merge([(int)$user['id']],scope_sql($user)[1]));
 ?><!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><meta name="theme-color" content="#f4f6fb"><meta name="csrf-token" content="<?=h(csrf())?>"><meta name="current-page" content="<?=h($page)?>"><meta name="poll-interval" content="<?=h(setting('refresh_seconds','5'))?>"><meta name="live-revision" content="<?=h(setting('live_revision_hash',''))?>"><meta name="last-synced" content="<?=h($last??'')?>"><title><?=h($title)?> · Speech Quality</title><link rel="stylesheet" href="assets/app.css?v=audit-qc-wide-20261110a"><link rel="stylesheet" href="assets/upgrade.css?v=audit-qc-wide-20261110a"><script defer src="assets/app.js?v=audit-qc-wide-20261110a"></script></head><body><div class="shell"><aside class="sidebar" id="sidebar"><a class="brand" href="<?=h(linkto('overview'))?>"><span class="brand-square">SQ</span><span><strong>Speech Quality</strong><small>SUPERVISION PORTAL</small></span></a><div class="sidebar-scroller"><p class="menu-caption">WORKSPACE</p><nav>
 <?=nav_item('overview','Overview','grid',$page)?>
 <?=nav_item('recordings','Recording history','list',$page)?>
 <?=nav_item('production','Production snapshot','chart',$page)?>
 <?=nav_item('contributors','Production contributors','users',$page)?>
 <?=nav_item('voices','Production voices','list',$page)?>
 <?=nav_item('review','Production review workspace','shield',$page)?>
 <?=nav_item('queue','Review queue','list',$page)?>
 <?=nav_item('followups','Follow-ups','follow',$page)?>
 <?=nav_item('reports','Daily reports','chart',$page)?>
 <?=nav_item('notifications','New recordings','bell',$page)?>
 </nav><?php if($admin):?><p class="menu-caption">ADMINISTRATION</p><nav>
 <?=nav_item('supervisors','Supervisors','users',$page)?>
 <?=nav_item('audit','Action log','history',$page)?>
 <?=nav_item('feedback','Feedback delivery','message',$page)?>
 <?=nav_item('integration','Integration status','sync',$page)?>
 <?=nav_item('settings','Settings','grid',$page)?>
 </nav><?php endif;?></div><a class="sidebar-account" href="<?=h(linkto('profile'))?>"><?=avatar_markup($user,'small')?><span><strong><?=h($user['display_name'])?></strong><small><?=h(ucfirst($user['role']))?> · Account</small></span></a></aside><div class="scrim" data-scrim></div><div class="main"><header class="topbar"><button class="icon-button" type="button" data-sidebar-toggle aria-label="Toggle navigation"><?=svg_icon('menu')?></button><span class="breadcrumbs">Speech Collector <span>/</span> <strong><?=h($title)?></strong></span><div class="top-spacer"></div><a class="top-search" href="<?=h(linkto('contributors'))?>"><?=svg_icon('search')?> Search contributors</a><?php if($admin):?><form method="post" action="<?=h(linkto('sync'))?>" class="sync-form"><?=csrf_input()?><button class="btn sync-button" type="submit"><?=svg_icon('sync')?> Sync now</button></form><?php endif;?><span data-sync-state class="sync-state <?=($last?'':'offline')?>" title="Last successful production synchronization"><?=($last?'Synced '.h(date_ml($last)):'Not yet connected')?></span><button class="icon-button" type="button" data-dark-toggle title="Toggle dark mode" aria-label="Toggle dark mode"><?=svg_icon('moon')?></button><a class="bell-button" href="<?=h(linkto('notifications'))?>" title="New recordings"><?=svg_icon('bell')?><b class="badge-count" <?=($seen===0?'hidden':'')?>><?=h((string)min(99,$seen)).($seen>99?'+':'')?></b></a><div class="account-dropdown"><button class="user-chip account-trigger" type="button" data-account-toggle aria-expanded="false" aria-haspopup="true" aria-controls="account-menu"><?=avatar_markup($user,'small')?><span><?=h($user['display_name'])?><small><?=h(ucfirst($user['role']))?></small></span><span class="chevron" aria-hidden="true"><svg viewBox="0 0 24 24" width="14" height="14" fill="none" stroke="currentColor" stroke-width="2.25" stroke-linecap="round" stroke-linejoin="round"><path d="m6 9 6 6 6-6"/></svg></span></button><div class="account-menu" id="account-menu" hidden><div class="account-menu-header"><?=avatar_markup($user,'small')?><span><strong><?=h($user['display_name'])?></strong><small><?=h($user['email'])?></small></span></div><a href="<?=h(linkto('profile'))?>">Profile & security</a><a href="<?=h(linkto('profile'))?>#change-password">Change password</a><form method="post" action="<?=h(linkto('logout'))?>"><?=csrf_input()?><button type="submit">Sign out</button></form></div></div></header><main class="content" id="main-content"><?=flash_html()?><?php if(!$ready):?><div class="connection-note"><strong>Production connection unavailable.</strong> This online portal needs an authorized production connection to display current recordings. <?php if($admin):?><a href="<?=h(linkto('integration'))?>">Integration details</a><?php endif;?></div><?php endif;?><?=$content?></main><footer class="footer"><span>Speech Quality · Secure supervision workspace</span><span>Ver. <?=APP_VERSION?> · Africa/Blantyre</span></footer></div></div></body></html><?php
}
function auth_page(string $title,string $content): void { ?><!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title><?=h($title)?> · Speech Quality</title><link rel="stylesheet" href="assets/app.css"></head><body class="auth-page"><div class="auth-card"><span class="brand-square">SQ</span><h1><?=h($title)?></h1><p>Speech Quality · Secure supervision portal</p><?=flash_html()?><?=$content?><div class="tiny">Ver. <?=APP_VERSION?> · Masakhane</div></div></body></html><?php }
