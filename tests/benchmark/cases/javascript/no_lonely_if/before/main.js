function classify(x) {
    if (x > 0) {
        return "pos";
    } else {
        if (x < 0) {
            return "neg";
        }
    }
    return "zero";
}
console.log(classify(1));
