desktop_conf = configuration_data()
desktop_conf.set('APP_ID', app_id)
desktop_conf.set('BINDIR', get_option('prefix') / get_option('bindir'))

configure_file(
  input: 'io.github.martinszeltins.Awaken.desktop.in',
  output: 'io.github.martinszeltins.Awaken.desktop',
  configuration: desktop_conf,
  install: true,
  install_dir: get_option('datadir') / 'applications',
)

metainfo_conf = configuration_data()
metainfo_conf.set('APP_ID', app_id)

configure_file(
  input: 'io.github.martinszeltins.Awaken.metainfo.xml.in',
  output: 'io.github.martinszeltins.Awaken.metainfo.xml',
  configuration: metainfo_conf,
  install: true,
  install_dir: get_option('datadir') / 'metainfo',
)

install_data(
  'icons/hicolor/256x256/apps/io.github.martinszeltins.Awaken.png',
  install_dir: get_option('datadir') / 'icons' / 'hicolor' / '256x256' / 'apps',
)

install_data(
  'icons/hicolor/symbolic/apps/io.github.martinszeltins.Awaken-symbolic.svg',
  install_dir: get_option('datadir') / 'icons' / 'hicolor' / 'symbolic' / 'apps',
)

install_data(
  'io.github.martinszeltins.Awaken.gschema.xml',
  install_dir: get_option('datadir') / 'glib-2.0' / 'schemas',
)

gnome.compile_schemas(
  depend_files: files('io.github.martinszeltins.Awaken.gschema.xml'),
)
