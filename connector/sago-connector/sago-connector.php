<?php
/**
 * Plugin Name: SAGO Connector
 * Description: The server side of SAGO (SEO, AEO, GEO, AIO optimization). A narrow, allowlisted REST API: every change is snapshotted first and can be restored, and anything not on the allowlist is refused.
 * Version: 1.0.0
 * Requires PHP: 7.4
 * License: AGPL-3.0-or-later
 *
 * Authenticate with a WordPress Application Password for a dedicated administrator
 * account. Every route requires manage_options.
 */

if ( ! defined( 'ABSPATH' ) ) {
	exit;
}

const SAGO_CONNECTOR_VERSION = '1.0.0';
const SAGO_MAX_SNAPSHOTS     = 50;

/* ------------------------------------------------------------------ settings applied at runtime */

/** REST requests do not load the admin includes; the plugin and file helpers live there. */
function sago_admin_includes() {
	require_once ABSPATH . 'wp-admin/includes/plugin.php';
	require_once ABSPATH . 'wp-admin/includes/file.php';
}

function sago_settings() {
	$s = get_option( 'sago_settings', array() );
	return is_array( $s ) ? $s : array();
}

// Form plugin fonts off: drop Forminator's own font stylesheets, whenever they are enqueued.
add_filter(
	'style_loader_tag',
	function ( $tag, $handle ) {
		$s = sago_settings();
		if ( ( $s['form_fonts'] ?? '' ) === 'theme' && 0 === strpos( $handle, 'forminator-font-' ) ) {
			return '';
		}
		return $tag;
	},
	10,
	2
);

// One schema owner: switch off Yoast's JSON-LD graph when the theme owns schema.
add_filter(
	'wpseo_json_ld_output',
	function ( $on ) {
		$s = sago_settings();
		return empty( $s['disable_yoast_schema'] ) ? $on : false;
	}
);

/* ------------------------------------------------------------------ abstract option keys */

function sago_litespeed_list( $key ) {
	$raw = get_option( 'litespeed.conf.' . $key, '' );
	if ( is_array( $raw ) ) {
		return array_values( array_filter( array_map( 'trim', $raw ) ) );
	}
	return array_values( array_filter( array_map( 'trim', preg_split( '/\r?\n/', (string) $raw ) ) ) );
}

function sago_litespeed_set_list( $key, $list ) {
	update_option( 'litespeed.conf.' . $key, implode( "\n", array_values( array_unique( $list ) ) ) );
}

const SAGO_SEPARATORS = array(
	'-' => 'sc-dash', '–' => 'sc-ndash', '—' => 'sc-mdash', ':' => 'sc-colon', '·' => 'sc-middot',
	'•' => 'sc-bull', '*' => 'sc-star', '|' => 'sc-pipe', '~' => 'sc-tilde', '«' => 'sc-laquo',
	'»' => 'sc-raquo', '<' => 'sc-lt', '>' => 'sc-gt',
);

function sago_plugin_file( $slug ) {
	sago_admin_includes();
	foreach ( array_keys( get_plugins() ) as $file ) {
		if ( dirname( $file ) === $slug || basename( $file, '.php' ) === $slug ) {
			return $file;
		}
	}
	return null;
}

/** Returns [get callable, set callable] for an allowlisted key, or null. */
function sago_option_handlers( $key ) {
	$map = array(
		'sago.form_fonts'           => array(
			function () { return sago_settings()['form_fonts'] ?? 'custom'; },
			function ( $v ) { $s = sago_settings(); $s['form_fonts'] = ( 'theme' === $v ) ? 'theme' : 'custom'; update_option( 'sago_settings', $s ); },
		),
		'sago.disable_yoast_schema' => array(
			function () { return ! empty( sago_settings()['disable_yoast_schema'] ); },
			function ( $v ) { $s = sago_settings(); $s['disable_yoast_schema'] = (bool) $v; update_option( 'sago_settings', $s ); },
		),
		'sago.delay_scripts'        => array(
			function () { return sago_litespeed_list( 'optm-js_delay_inc' ); },
			function ( $v ) { sago_litespeed_set_list( 'optm-js_delay_inc', (array) $v ); },
		),
		'sago.css_excludes'         => array(
			function () { return sago_litespeed_list( 'optm-css_exc' ); },
			function ( $v ) { sago_litespeed_set_list( 'optm-css_exc', (array) $v ); },
		),
		'sago.title_separator'      => array(
			function () {
				$t = get_option( 'wpseo_titles', array() );
				$flip = array_flip( SAGO_SEPARATORS );
				return $flip[ $t['separator'] ?? 'sc-dash' ] ?? '-';
			},
			function ( $v ) {
				if ( ! isset( SAGO_SEPARATORS[ $v ] ) ) {
					throw new InvalidArgumentException( 'unknown separator' );
				}
				$t = get_option( 'wpseo_titles', array() );
				$t['separator'] = SAGO_SEPARATORS[ $v ];
				update_option( 'wpseo_titles', $t );
			},
		),
		'sago.disable_saswp'        => array(
			function () {
				$f = sago_plugin_file( 'schema-and-structured-data-for-wp' );
				return $f ? ! is_plugin_active( $f ) : true;
			},
			function ( $v ) {
				$f = sago_plugin_file( 'schema-and-structured-data-for-wp' );
				if ( ! $f ) {
					return;
				}
				if ( $v ) {
					deactivate_plugins( $f, true );
				} else {
					activate_plugin( $f, '', false, true );
				}
			},
		),
	);
	return $map[ $key ] ?? null;
}

const SAGO_PLUGINS_ALLOWED = array( 'contact-form-7', 'schema-and-structured-data-for-wp', 'flamingo' );

/* ------------------------------------------------------------------ snapshots */

function sago_snapshot( $kind, $data ) {
	$all = get_option( 'sago_snapshots', array() );
	$id  = 'snap_' . gmdate( 'Ymd_His' ) . '_' . wp_generate_password( 6, false );
	$all[ $id ] = array( 'kind' => $kind, 'data' => $data, 'created' => gmdate( 'c' ) );
	if ( count( $all ) > SAGO_MAX_SNAPSHOTS ) {
		$all = array_slice( $all, -SAGO_MAX_SNAPSHOTS, null, true );
	}
	update_option( 'sago_snapshots', $all, false );
	return $id;
}

function sago_forbidden( $msg ) {
	return new WP_Error( 'sago_forbidden', $msg, array( 'status' => 403 ) );
}

/* ------------------------------------------------------------------ theme deploy */

function sago_theme_dir() {
	return get_stylesheet_directory();
}

function sago_theme_version() {
	return wp_get_theme()->get( 'Version' );
}

function sago_backup_dir() {
	$u = wp_upload_dir();
	$d = trailingslashit( $u['basedir'] ) . 'sago-backups';
	if ( ! is_dir( $d ) ) {
		wp_mkdir_p( $d );
		file_put_contents( $d . '/index.php', '<?php // Silence.' );
		file_put_contents( $d . '/.htaccess', "Require all denied\nDeny from all\n" );
	}
	return $d;
}

function sago_copy_dir( $src, $dst ) {
	wp_mkdir_p( $dst );
	$it = new RecursiveIteratorIterator( new RecursiveDirectoryIterator( $src, FilesystemIterator::SKIP_DOTS ), RecursiveIteratorIterator::SELF_FIRST );
	foreach ( $it as $item ) {
		$target = $dst . DIRECTORY_SEPARATOR . $it->getSubPathName();
		if ( $item->isDir() ) {
			wp_mkdir_p( $target );
		} else {
			copy( $item->getPathname(), $target );
		}
	}
}

function sago_rm_dir( $dir ) {
	if ( ! is_dir( $dir ) ) {
		return;
	}
	$it = new RecursiveIteratorIterator( new RecursiveDirectoryIterator( $dir, FilesystemIterator::SKIP_DOTS ), RecursiveIteratorIterator::CHILD_FIRST );
	foreach ( $it as $item ) {
		$item->isDir() ? rmdir( $item->getPathname() ) : unlink( $item->getPathname() );
	}
	rmdir( $dir );
}

function sago_deploy_theme( $zip_b64 ) {
	$bytes = base64_decode( $zip_b64, true );
	if ( false === $bytes || strlen( $bytes ) > 20 * 1024 * 1024 ) {
		return sago_forbidden( 'zip missing or larger than 20 MB' );
	}
	$slug    = basename( sago_theme_dir() );
	$tmp_zip = wp_tempnam( 'sago-theme.zip' );
	file_put_contents( $tmp_zip, $bytes );
	$zip = new ZipArchive();
	if ( true !== $zip->open( $tmp_zip ) ) {
		return new WP_Error( 'sago_bad_zip', 'not a zip', array( 'status' => 400 ) );
	}
	for ( $i = 0; $i < $zip->numFiles; $i++ ) {
		$name = $zip->getNameIndex( $i );
		if ( 0 !== strpos( $name, $slug . '/' ) || false !== strpos( $name, '..' ) ) {
			$zip->close();
			return sago_forbidden( "zip entries must sit under {$slug}/: {$name}" );
		}
	}
	$staging = trailingslashit( get_theme_root() ) . '.sago-staging-' . wp_generate_password( 6, false );
	wp_mkdir_p( $staging );
	$zip->extractTo( $staging );
	$zip->close();
	unlink( $tmp_zip );
	if ( ! file_exists( $staging . '/' . $slug . '/style.css' ) ) {
		sago_rm_dir( $staging );
		return new WP_Error( 'sago_bad_theme', 'style.css missing', array( 'status' => 400 ) );
	}
	$before = sago_theme_version();
	$backup = sago_backup_dir() . '/' . $slug . '-' . gmdate( 'Ymd_His' );
	sago_copy_dir( sago_theme_dir(), $backup );
	$snap = sago_snapshot( 'theme', array( 'backup' => $backup, 'slug' => $slug, 'version' => $before ) );
	// Swap: move the live theme aside, move the new one in; restore on any failure.
	$live  = sago_theme_dir();
	$aside = $live . '.sago-old';
	sago_rm_dir( $aside );
	if ( ! rename( $live, $aside ) ) {
		sago_rm_dir( $staging );
		return new WP_Error( 'sago_swap_failed', 'could not move the live theme aside', array( 'status' => 500 ) );
	}
	if ( ! rename( $staging . '/' . $slug, $live ) ) {
		rename( $aside, $live );
		sago_rm_dir( $staging );
		return new WP_Error( 'sago_swap_failed', 'could not move the new theme in; live theme restored', array( 'status' => 500 ) );
	}
	sago_rm_dir( $aside );
	sago_rm_dir( $staging );
	wp_clean_themes_cache();
	if ( function_exists( 'opcache_reset' ) ) {
		opcache_reset();
	}
	$after = wp_get_theme( $slug )->get( 'Version' );
	return array( 'before' => $before, 'after' => $after, 'snapshot' => $snap );
}

function sago_restore( $id ) {
	$all = get_option( 'sago_snapshots', array() );
	if ( empty( $all[ $id ] ) ) {
		return new WP_Error( 'sago_no_snapshot', 'unknown snapshot', array( 'status' => 404 ) );
	}
	$s = $all[ $id ];
	if ( 'theme' === $s['kind'] ) {
		$live = trailingslashit( get_theme_root() ) . $s['data']['slug'];
		if ( ! is_dir( $s['data']['backup'] ) ) {
			return new WP_Error( 'sago_backup_missing', 'theme backup is missing', array( 'status' => 500 ) );
		}
		$aside = $live . '.sago-failed';
		sago_rm_dir( $aside );
		rename( $live, $aside );
		sago_copy_dir( $s['data']['backup'], $live );
		sago_rm_dir( $aside );
		wp_clean_themes_cache();
		if ( function_exists( 'opcache_reset' ) ) {
			opcache_reset();
		}
		return array( 'restored' => $id, 'version' => sago_theme_version() );
	}
	if ( 'option' === $s['kind'] ) {
		$h = sago_option_handlers( $s['data']['key'] );
		if ( $h ) {
			$h[1]( $s['data']['value'] );
		}
		return array( 'restored' => $id );
	}
	return new WP_Error( 'sago_bad_snapshot', 'unknown snapshot kind', array( 'status' => 500 ) );
}

/* ------------------------------------------------------------------ REST routes */

add_action(
	'rest_api_init',
	function () {
		$ns   = 'sago/v1';
		$perm = function () {
			sago_admin_includes();
			return current_user_can( 'manage_options' );
		};

		register_rest_route( $ns, '/status', array( 'methods' => 'GET', 'permission_callback' => $perm, 'callback' => function () {
			return array(
				'connector'     => SAGO_CONNECTOR_VERSION,
				'wp'            => get_bloginfo( 'version' ),
				'theme'         => get_stylesheet(),
				'theme_version' => sago_theme_version(),
				'litespeed'     => defined( 'LSCWP_V' ),
				'yoast'         => defined( 'WPSEO_VERSION' ),
			);
		} ) );

		register_rest_route( $ns, '/option', array(
			array( 'methods' => 'GET', 'permission_callback' => $perm, 'callback' => function ( WP_REST_Request $r ) {
				$h = sago_option_handlers( (string) $r['key'] );
				return $h ? array( 'key' => $r['key'], 'value' => $h[0]() ) : sago_forbidden( 'key not on the allowlist' );
			} ),
			array( 'methods' => 'POST', 'permission_callback' => $perm, 'callback' => function ( WP_REST_Request $r ) {
				$key = (string) $r['key'];
				$h   = sago_option_handlers( $key );
				if ( ! $h ) {
					return sago_forbidden( 'key not on the allowlist' );
				}
				$before = $h[0]();
				$value  = $r['value'];
				if ( is_array( $value ) && isset( $value['add'] ) ) {
					$value = array_values( array_unique( array_merge( (array) $before, (array) $value['add'] ) ) );
				}
				$snap = sago_snapshot( 'option', array( 'key' => $key, 'value' => $before ) );
				try {
					$h[1]( $value );
				} catch ( Exception $e ) {
					return new WP_Error( 'sago_bad_value', $e->getMessage(), array( 'status' => 400 ) );
				}
				return array( 'before' => $before, 'after' => $h[0](), 'snapshot' => $snap );
			} ),
		) );

		register_rest_route( $ns, '/plugin', array(
			array( 'methods' => 'GET', 'permission_callback' => $perm, 'callback' => function ( WP_REST_Request $r ) {
				$f = sago_plugin_file( (string) $r['slug'] );
				return array( 'slug' => $r['slug'], 'active' => $f ? is_plugin_active( $f ) : false );
			} ),
			array( 'methods' => 'POST', 'permission_callback' => $perm, 'callback' => function ( WP_REST_Request $r ) {
				$slug = (string) $r['slug'];
				if ( ! in_array( $slug, SAGO_PLUGINS_ALLOWED, true ) ) {
					return sago_forbidden( 'plugin not on the allowlist' );
				}
				$f = sago_plugin_file( $slug );
				if ( ! $f ) {
					return new WP_Error( 'sago_no_plugin', 'plugin not installed', array( 'status' => 404 ) );
				}
				$before = is_plugin_active( $f );
				$r['active'] ? activate_plugin( $f, '', false, true ) : deactivate_plugins( $f, true );
				return array( 'before' => $before, 'after' => is_plugin_active( $f ) );
			} ),
		) );

		register_rest_route( $ns, '/purge', array( 'methods' => 'POST', 'permission_callback' => $perm, 'callback' => function ( WP_REST_Request $r ) {
			$urls = array_slice( array_filter( (array) $r['urls'], 'is_string' ), 0, 500 );
			$home = home_url();
			$done = 0;
			foreach ( $urls as $u ) {
				if ( 0 !== strpos( $u, $home ) ) {
					continue;
				}
				do_action( 'litespeed_purge_url', $u );
				$pid = url_to_postid( $u );
				if ( $pid ) {
					clean_post_cache( $pid );
				}
				$done++;
			}
			return array( 'purged' => $done );
		} ) );

		register_rest_route( $ns, '/theme', array( 'methods' => 'POST', 'permission_callback' => $perm, 'callback' => function ( WP_REST_Request $r ) {
			return sago_deploy_theme( (string) $r['zip'] );
		} ) );

		register_rest_route( $ns, '/restore', array( 'methods' => 'POST', 'permission_callback' => $perm, 'callback' => function ( WP_REST_Request $r ) {
			return sago_restore( (string) $r['snapshot'] );
		} ) );

		register_rest_route( $ns, '/content-types', array( 'methods' => 'GET', 'permission_callback' => $perm, 'callback' => function () {
			$types = get_post_types( array( 'public' => true ), 'names' );
			unset( $types['attachment'] );
			return array_values( $types );
		} ) );

		register_rest_route( $ns, '/permalinks', array( 'methods' => 'GET', 'permission_callback' => $perm, 'callback' => function () {
			$types = get_post_types( array( 'public' => true ), 'names' );
			unset( $types['attachment'] );
			$ids = get_posts( array( 'post_type' => array_values( $types ), 'post_status' => 'publish', 'numberposts' => -1, 'fields' => 'ids' ) );
			$out = array();
			foreach ( $ids as $id ) {
				$out[ (string) $id ] = get_permalink( $id );
			}
			return $out;
		} ) );
	}
);
