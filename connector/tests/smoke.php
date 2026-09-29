<?php
require '/wordpress/wp-load.php';
wp_set_current_user(1);
$out = array();
function sago_call($m, $route, $params = array()) {
    $r = new WP_REST_Request($m, $route);
    foreach ($params as $k => $v) { $r->set_param($k, $v); }
    $res = rest_do_request($r);
    return array('status' => $res->get_status(), 'data' => $res->get_data());
}
$out['status'] = sago_call('GET', '/sago/v1/status');
$out['sep_set'] = sago_call('POST', '/sago/v1/option', array('key' => 'sago.title_separator', 'value' => '|'));
$out['sep_raw'] = get_option('wpseo_titles');
$out['sep_get'] = sago_call('GET', '/sago/v1/option', array('key' => 'sago.title_separator'));
$out['forbidden'] = sago_call('POST', '/sago/v1/option', array('key' => 'siteurl', 'value' => 'https://evil.test'));
$out['siteurl_unchanged'] = get_option('siteurl');
$out['delay_add'] = sago_call('POST', '/sago/v1/option', array('key' => 'sago.delay_scripts', 'value' => array('add' => array('recaptcha/api.js'))));
$out['delay_raw'] = get_option('litespeed.conf.optm-js_delay_inc');
$out['restore_opt'] = sago_call('POST', '/sago/v1/restore', array('snapshot' => $out['delay_add']['data']['snapshot']));
$out['delay_after_restore'] = sago_call('GET', '/sago/v1/option', array('key' => 'sago.delay_scripts'));
$out['fonts'] = sago_call('POST', '/sago/v1/option', array('key' => 'sago.form_fonts', 'value' => 'theme'));
$out['yoast_off'] = sago_call('POST', '/sago/v1/option', array('key' => 'sago.disable_yoast_schema', 'value' => true));
$out['yoast_filter'] = apply_filters('wpseo_json_ld_output', true);
$out['font_tag'] = apply_filters('style_loader_tag', '<link id="forminator-font-roboto-css">', 'forminator-font-roboto');
$slug = get_stylesheet(); $dir = get_stylesheet_directory();
$out['theme'] = $slug; $out['version_before'] = wp_get_theme($slug)->get('Version');
$zipf = sys_get_temp_dir() . '/t.zip';
$z = new ZipArchive(); $z->open($zipf, ZipArchive::CREATE | ZipArchive::OVERWRITE);
$it = new RecursiveIteratorIterator(new RecursiveDirectoryIterator($dir, FilesystemIterator::SKIP_DOTS));
foreach ($it as $f) {
    $rel = substr($f->getPathname(), strlen($dir) + 1);
    $c = file_get_contents($f->getPathname());
    if ($rel === 'style.css') { $c = preg_replace('/^Version:.*$/m', 'Version: 9.9.9', $c); }
    $z->addFromString($slug . '/' . $rel, $c);
}
$z->close();
$out['deploy'] = sago_call('POST', '/sago/v1/theme', array('zip' => base64_encode(file_get_contents($zipf))));
wp_clean_themes_cache();
$out['version_after_deploy'] = (new WP_Theme($slug, get_theme_root()))->get('Version');
$out['restore_theme'] = sago_call('POST', '/sago/v1/restore', array('snapshot' => $out['deploy']['data']['snapshot'] ?? ''));
$out['version_after_restore'] = (new WP_Theme($slug, get_theme_root()))->get('Version');
$bad = sys_get_temp_dir() . '/bad.zip';
$z = new ZipArchive(); $z->open($bad, ZipArchive::CREATE | ZipArchive::OVERWRITE);
$z->addFromString('../../wp-config.php', '<?php // pwned'); $z->close();
$out['bad_zip'] = sago_call('POST', '/sago/v1/theme', array('zip' => base64_encode(file_get_contents($bad))));
$out['plugin_forbidden'] = sago_call('POST', '/sago/v1/plugin', array('slug' => 'akismet', 'active' => false));
$out['content_types'] = sago_call('GET', '/sago/v1/content-types');
$out['permalinks_count'] = count(sago_call('GET', '/sago/v1/permalinks')['data']);
wp_set_current_user(0);
$out['anon'] = sago_call('GET', '/sago/v1/status');
file_put_contents('/results/out.json', json_encode($out, JSON_PRETTY_PRINT | JSON_UNESCAPED_SLASHES));
