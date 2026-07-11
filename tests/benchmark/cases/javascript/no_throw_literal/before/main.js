function validate(x) {
    if (!x) {
        throw "invalid";
    }
    return x;
}
validate(1);
