project(
  'startup-applications',
  version: '1.0.0',
  meson_version: '>= 0.59.0',
)

gnome = import('gnome')
python = import('python')

py_installation = python.find_installation('python3', modules: ['gi'])
dependency('gtk4', version: '>= 4.12')
dependency('libadwaita-1', version: '>= 1.5')

app_id = 'io.github.martinszeltins.Awaken'
pkgdatadir = get_option('prefix') / get_option('datadir') / meson.project_name()

conf = configuration_data()
conf.set('PYTHON', py_installation.full_path())
conf.set('VERSION', meson.project_version())
conf.set('pkgdatadir', pkgdatadir)

configure_file(
  input: 'src/awaken.in',
  output: 'awaken',
  configuration: conf,
  install: true,
  install_dir: get_option('bindir'),
  install_mode: 'rwxr-xr-x',
)

install_data('docker-icon.png', install_dir: pkgdatadir)

install_data(
  [
    'src/startup_applications/__init__.py',
    'src/startup_applications/__main__.py',
    'src/startup_applications/application.py',
    'src/startup_applications/const.py',
    'src/startup_applications/main.py',
    'src/startup_applications/scanner.py',
    'src/startup_applications/window.py',
  ],
  install_dir: pkgdatadir / 'startup_applications',
)

devenv = environment()
devenv.prepend('PYTHONPATH', meson.project_source_root() / 'src')
devenv.prepend('XDG_DATA_DIRS', meson.project_source_root() / 'data')
meson.add_devenv(devenv)

subdir('data')

gnome.post_install(
  glib_compile_schemas: true,
  gtk_update_icon_cache: true,
  update_desktop_database: true,
)
