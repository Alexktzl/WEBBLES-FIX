package main
func main() {
    x := 1
    if true {
        x := 2
        _ = x
    }
    _ = x
}
