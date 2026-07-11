function hasKey(obj, key) {
    if (!(key in obj)) {
        return false;
    }
    return true;
}
console.log(hasKey({a: 1}, "a"));
