export default () => {
  return {
    map: false,
    plugins: {
      autoprefixer: {
        cascade: false
      },
      'postcss-combine-duplicated-selectors': {},
      'postcss-drop-empty-css-vars': {}
    }
  }
}
